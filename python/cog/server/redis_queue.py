import datetime
import io
import json
import os
import queue
import signal
import sys
import time
import traceback
from argparse import ArgumentParser
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Thread
from mimetypes import guess_type
from typing import Any, Callable, Dict, Iterable, Optional, Tuple, List
from urllib.parse import urlparse

import boto3
from boto3_type_annotations.s3 import ServiceResource
from botocore.config import Config
import redis
import requests
import uuid
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ..files import guess_filename
from ..predictor import (
    get_input_type,
    get_predictor_ref,
    load_config,
    load_predictor_from_ref,
)
from ..schema import Status, WebhookEvent
from .eventtypes import Done, Heartbeat, Log, PredictionOutput, PredictionOutputType
from .probes import ProbeHelper
from .webhook import requests_session, webhook_caller
from .worker import Worker
from ..types import Path
from PIL import Image
from io import BytesIO


# A class for holding prediction output data for upload queue
class UploadObject:
    def __init__(
        self,
        image_bytes: bytes,
        target_extension: str,
        target_quality: int,
    ):
        self.image_bytes = image_bytes
        self.target_extension = target_extension
        self.target_quality = target_quality


class RedisQueueWorker:
    SETUP_TIME_QUEUE_SUFFIX = "-setup-time"
    RUN_TIME_QUEUE_SUFFIX = "-run-time"
    STAGE_SETUP = "setup"
    STAGE_RUN = "run"

    def __init__(
        self,
        predictor_ref: str,
        redis_url: str,
        input_queue: str,
        s3_client: ServiceResource,
        s3_bucket: str,
        consumer_id: str,
        predict_timeout: Optional[int] = None,
        report_setup_run_url: Optional[str] = None,
        max_failure_count: Optional[int] = None,
    ):
        # We want to do upload on a separate thread so use a queue to
        self.upload_queue: queue.Queue[Dict[str, Any]] = queue.Queue()
        self.worker = Worker(predictor_ref)
        self.redis_url = redis_url
        self.input_queue = input_queue
        self.s3_client = s3_client
        self.s3_bucket = s3_bucket
        self.consumer_id = consumer_id
        self.predict_timeout = predict_timeout
        self.report_setup_run_url = report_setup_run_url
        self.max_failure_count = max_failure_count
        if self.predict_timeout is not None:
            # 30s grace period allows final responses to be sent and job to be acked
            self.autoclaim_messages_after = self.predict_timeout + 30
        else:
            # retry after 10 minutes by default
            self.autoclaim_messages_after = 10 * 60

        # Set up types
        predictor = load_predictor_from_ref(predictor_ref)
        self.InputType = get_input_type(predictor)

        self.redis = redis.from_url(self.redis_url)
        self.should_exit = False
        self.setup_time_queue = input_queue + self.SETUP_TIME_QUEUE_SUFFIX
        self.predict_time_queue = input_queue + self.RUN_TIME_QUEUE_SUFFIX
        self.stats_queue_length = 100
        self.tracer = trace.get_tracer("cog")
        self.probes = ProbeHelper()

        sys.stderr.write(
            f"Connected to Redis: {self.redis.get_connection_kwargs().get('host')}\n"
        )

    def receive_message(self) -> Tuple[Optional[str], Optional[str]]:
        # first, try to autoclaim old messages from pending queue
        raw_messages = self.redis.execute_command(
            "XAUTOCLAIM",
            self.input_queue,
            self.input_queue,
            self.consumer_id,
            str(self.autoclaim_messages_after * 1000),
            "0-0",
            "COUNT",
            1,
        )
        # format: [[b'1619393873567-0', [b'mykey', b'myval']]]
        # since redis==4.3.4 an empty response from xautoclaim is indicated by [[b'0-0', []]]
        if raw_messages and raw_messages[0] is not None and len(raw_messages[0]) == 2:
            key, raw_message = raw_messages[0]
            assert raw_message[0] == b"value"
            return key.decode(), raw_message[1].decode()

        # if no old messages exist, get message from main queue
        raw_messages = self.redis.xreadgroup(
            groupname=self.input_queue,
            consumername=self.consumer_id,
            streams={self.input_queue: ">"},
            count=1,
            block=1000,
        )
        if not raw_messages:
            return None, None

        # format: [[b'mystream', [(b'1619395583065-0', {b'mykey': b'myval6'})]]]
        key, raw_message = raw_messages[0][1][0]
        return key.decode(), raw_message[b"value"].decode()

    def start_upload_thread(self) -> None:
        """Reads upload queue, uploads files, sends response to redis or webhook like normal"""
        sys.stderr.write("Starting upload thread...\n")
        while not self.should_exit:
            uploadMsg: Dict[str, Any] = self.upload_queue.get()
            if "upload_outputs" in uploadMsg:
                upload_outputs: List[UploadObject] = uploadMsg["upload_outputs"]
                try:
                    uploadMsg["output"] = self.upload_files(
                        upload_outputs, uploadMsg["upload_prefix"]
                    )
                except Exception as e:
                    sys.stderr.write(f"Error uploading files: {e}\n")
                    uploadMsg["status"] = Status.FAILED
                    uploadMsg["error"] = str(e)
                finally:
                    if "upload_outputs" in uploadMsg:
                        del uploadMsg["upload_outputs"]
                    self.send_response(uploadMsg)

    def start(self) -> None:
        sys.stderr.write("Starting worker... Feb 5th Edition\n")
        with self.tracer.start_as_current_span(name="redis_queue.setup") as span:
            started_at = datetime.datetime.now()

            setup_logs = ""
            try:
                for event in self.worker.setup():
                    if isinstance(event, Log):
                        setup_logs += event.message
                    elif isinstance(event, Done):
                        setup_status = (
                            Status.FAILED if event.error else Status.SUCCEEDED
                        )
            except Exception:
                setup_status = Status.FAILED

            if setup_status == Status.FAILED:
                sys.stderr.write("Setup failed, exiting immediately")
                self.should_exit = True

            completed_at = datetime.datetime.now()

            # Signal pod readiness (when in k8s)
            self.probes.ready()

            if self.report_setup_run_url:
                # TODO this should be async so we can get on with predictions ASAP
                requests_session().post(
                    self.report_setup_run_url,
                    json={
                        "status": setup_status,
                        "started_at": format_datetime(started_at),
                        "completed_at": format_datetime(completed_at),
                        "logs": setup_logs,
                    },
                )

            # TODO deprecate this
            setup_time = (completed_at - started_at).total_seconds()
            self.redis.xadd(
                self.setup_time_queue,
                fields={"duration": setup_time},
                maxlen=self.stats_queue_length,
            )
            sys.stderr.write(f"Setup time: {setup_time:.2f}\n")

        failure_count = 0

        sys.stderr.write(f"Waiting for message on {self.input_queue}\n")
        while not self.should_exit:
            try:
                message_id, message_json = self.receive_message()
                if message_json is None:
                    # tight loop in order to respect self.should_exit
                    continue

                time_in_queue = calculate_time_in_queue(message_id)  # type: ignore
                message = json.loads(message_json)

                # Check whether the incoming message includes details of an
                # OpenTelemetry trace, to make distributed tracing work. The
                # value should look like:
                #
                #     00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01
                if "traceparent" in message:
                    context = TraceContextTextMapPropagator().extract(
                        {"traceparent": message["traceparent"]}
                    )
                else:
                    context = None

                with self.tracer.start_as_current_span(
                    name="redis_queue.process_message",
                    context=context,
                    attributes={"time_in_queue": time_in_queue},
                ) as span:
                    webhook = message.get("webhook")
                    if webhook is not None:
                        self.send_response = webhook_caller(webhook)
                    else:
                        redis_key = message["redis_pubsub_key"]
                        self.send_response = self.redis_publisher(redis_key)

                    sys.stderr.write(
                        f"Received message {message_id} on {self.input_queue}\n"
                    )

                    should_cancel = self.cancelation_checker(message.get("cancel_key"))

                    if "webhook_events_filter" in message:
                        valid_events = {ev.value for ev in WebhookEvent}

                        for event in message["webhook_events_filter"]:
                            if event not in valid_events:
                                raise ValueError(
                                    f"Invalid webhook event {event}! Must be one of {valid_events}"
                                )

                        # We always send the completed event
                        events_filter = set(message["webhook_events_filter"]) | {
                            WebhookEvent.COMPLETED
                        }
                    else:
                        events_filter = WebhookEvent.default_events()

                    for response_event, response in self.run_prediction(
                        message, should_cancel
                    ):
                        if (
                            "upload_outputs" in response
                            and len(response["upload_outputs"]) > 0
                        ):
                            self.upload_queue.put(response)
                        elif response_event in events_filter:
                            self.send_response(response)

                    if self.max_failure_count is not None:
                        # Keep track of runs of failures to catch the situation
                        # where the worker has gotten into a bad state where it can
                        # only fail predictions, but isn't exiting.
                        if response["status"] == Status.FAILED:
                            failure_count += 1
                            if failure_count > self.max_failure_count:
                                self.should_exit = True
                                print(
                                    f"Had {failure_count} failures in a row, exiting...",
                                    file=sys.stderr,
                                )
                        else:
                            failure_count = 0

                    self.redis.xack(self.input_queue, self.input_queue, message_id)
                    self.redis.xdel(self.input_queue, message_id)

            except Exception as e:
                tb = traceback.format_exc()
                sys.stderr.write(f"Failed to handle message: {tb}\n")

        sys.stderr.write("Shutting down worker: bye bye!\n")
        self.worker.shutdown()

    def run_prediction(
        self, message: Dict[str, Any], should_cancel: Callable
    ) -> Iterable[Tuple[WebhookEvent, Dict[str, Any]]]:
        # use the request message as the basis of our response so
        # that we echo back any additional fields sent to us
        response = message
        response["status"] = Status.PROCESSING
        response["output"] = None
        response["logs"] = ""

        started_at = datetime.datetime.now()

        try:
            input_obj = self.InputType(**response["input"])
        except Exception as e:
            response["status"] = Status.FAILED
            response["error"] = str(e)
            yield (WebhookEvent.COMPLETED, response)

            try:
                input_obj.cleanup()
            except Exception as e:
                sys.stderr.write(f"Cleanup function caught error: {e}")

            return

        response["started_at"] = format_datetime(started_at)
        response["logs"] = ""

        yield (WebhookEvent.START, response)

        timed_out = False
        was_canceled = False
        done_event = None
        output_type = None
        had_error = False

        # If we have outputs that we need to upload
        response["upload_outputs"] = []

        try:
            for event in self.worker.predict(payload=input_obj.dict(), poll=0.1):
                if not was_canceled and should_cancel():
                    was_canceled = True
                    self.worker.cancel()

                if not timed_out and self.predict_timeout:
                    runtime = (datetime.datetime.now() - started_at).total_seconds()
                    if runtime > self.predict_timeout:
                        timed_out = True
                        self.worker.cancel()

                if isinstance(event, Heartbeat):
                    # Heartbeat events exist solely to ensure that we have a
                    # regular opportunity to check for cancelation and
                    # timeouts.
                    #
                    # We don't need to do anything with them.
                    pass
                elif isinstance(event, Log):
                    response["logs"] += event.message
                    yield (WebhookEvent.LOGS, response)
                elif isinstance(event, PredictionOutputType):
                    # Note: this error message will be seen by users so it is
                    # intentionally vague about what has gone wrong.
                    assert output_type is None, "Predictor returned unexpected output"
                    output_type = event
                    if output_type.multi:
                        response["output"] = []
                elif isinstance(event, PredictionOutput):
                    # Note: this error message will be seen by users so it is
                    # intentionally vague about what has gone wrong.
                    assert (
                        output_type is not None
                    ), "Predictor returned unexpected output"

                    try:
                        response["upload_prefix"] = ""
                        if "upload_path_prefix" in input_obj.dict():
                            response["upload_prefix"] = input_obj.dict()[
                                "upload_path_prefix"
                            ]

                        if (
                            event.payload["nsfw_count"] == 0
                            and len(event.payload["outputs"]) == 0
                        ):
                            raise Exception("Missing outputs and nsfw_count")

                        # Sometimes we could have, 0 outputs but a >0 nsfw_count
                        response["output"] = []
                        if len(event.payload["outputs"]) > 0:
                            # Copy files to memory
                            for output in event.payload["outputs"]:
                                response["upload_outputs"].append(
                                    UploadObject(
                                        image_bytes=output["image_bytes"],
                                        target_quality=output["target_quality"],
                                        target_extension=output["target_extension"],
                                    )
                                )
                        response["nsfw_count"] = event.payload["nsfw_count"]
                    except Exception as e:
                        sys.stderr.write(f"Error uploading files to S3: {e}\n")
                        had_error = True

                elif isinstance(event, Done):
                    assert not done_event, "Predictor unexpectedly returned done twice"
                    done_event = event
                else:
                    sys.stderr.write(f"Received unexpected event from worker: {event}")

            completed_at = datetime.datetime.now()
            response["completed_at"] = format_datetime(completed_at)

            # It should only be possible to get here if we got a done event.
            assert done_event

            if had_error:
                response["status"] = Status.FAILED
                response["error"] = "Error uploading files"
            elif done_event.canceled and was_canceled:
                response["status"] = Status.CANCELED
            elif done_event.canceled and timed_out:
                response["status"] = Status.FAILED
                response["error"] = "Prediction timed out"
            elif done_event.error:
                response["status"] = Status.FAILED
                response["error"] = str(done_event.error_detail)
            else:
                response["status"] = Status.SUCCEEDED
                response["metrics"] = {
                    "predict_time": (completed_at - started_at).total_seconds()
                }
        except Exception as e:
            sys.stderr.write(f"Error in prediction: {e}\n")
            completed_at = datetime.datetime.now()
            response["completed_at"] = format_datetime(completed_at)
            response["status"] = Status.FAILED
            response["error"] = str(e)
        finally:
            yield (WebhookEvent.COMPLETED, response)

            try:
                input_obj.cleanup()
            except Exception as e:
                print(f"Cleanup function caught error: {e}", file=sys.stderr)

    def download(self, url: str) -> bytes:
        resp = requests.get(url)
        resp.raise_for_status()
        return resp.content

    # Publish to a redis channel
    def redis_publisher(self, redis_key: str) -> Callable:
        def setter(response: Any) -> None:
            self.redis.publish(redis_key, json.dumps(response))

        return setter

    def cancelation_checker(self, redis_key: str) -> Callable:
        def checker() -> bool:
            return redis_key is not None and self.redis.exists(redis_key) > 0

        return checker

    def parse_content_type(self, extension: str) -> Optional[str]:
        if extension == ".jpeg" or extension == ".jpg":
            return "image/jpeg"
        elif extension == ".png":
            return "image/png"
        elif extension == ".webp":
            return "image/webp"

        return None

    def convert_bytes_to_target(
        self, image_bytes: bytes, target_extension: str, target_quality: int
    ) -> bytes:
        img_format = target_extension[1:].upper()
        image = Image.open(BytesIO(image_bytes))

        converted_bytes = BytesIO()
        image.save(converted_bytes, format=img_format, quality=target_quality)
        converted_bytes.seek(0)

        result = converted_bytes.read()
        converted_bytes.close()
        return result

    def convert_and_upload_to_s3(
        self,
        image_bytes: bytes,
        target_quality: int,
        target_extension: str,
        upload_path_prefix: str,
    ) -> str:
        start_conv = time.time()
        converted_bytes = self.convert_bytes_to_target(
            image_bytes, target_extension, target_quality
        )
        end_conv = time.time()
        print(
            f"Converted image in: {round((end_conv - start_conv) *1000)} ms - {target_extension} - {target_quality}"
        )
        key = f"{str(uuid.uuid4())}{target_extension}"
        if upload_path_prefix is not None and upload_path_prefix != "":
            key = f"{ensure_trailing_slash(upload_path_prefix)}{key}"

        content_type = self.parse_content_type(target_extension)
        start_upload = time.time()
        self.s3_client.Bucket(self.s3_bucket).put_object(
            Body=converted_bytes, Key=key, ContentType=content_type
        )
        end_upload = time.time()
        print(f"Uploaded image in: {round((end_upload - start_upload) *1000)} ms")

        return f"s3://{self.s3_bucket}/{key}"

    def upload_files(
        self, uploadObjects: List[UploadObject], upload_path_prefix: str
    ) -> Iterable[str]:
        print("Started - Upload all files to S3 in parallel and return the S3 URLs")
        start = time.time()

        # Run all uploads at same time in threadpool
        tasks: List[Future] = []
        with ThreadPoolExecutor(max_workers=len(uploadObjects)) as executor:
            for uo in uploadObjects:
                tasks.append(
                    executor.submit(
                        self.convert_and_upload_to_s3,
                        uo.image_bytes,
                        uo.target_quality,
                        uo.target_extension,
                        upload_path_prefix,
                    )
                )

        # Get results
        results = []
        for task in tasks:
            results.append(task.result())

        end = time.time()
        print(
            f"📤 All converted and uploaded to S3 in: {round((end - start) *1000)} ms 📤"
        )

        return results


def calculate_time_in_queue(message_id: str) -> float:
    """
    Calculate how long a message spent in the queue based on the timestamp in
    the message ID.
    """
    now = time.time()
    queue_time = int(message_id[:13]) / 1000.0
    return now - queue_time


def format_datetime(timestamp: datetime.datetime) -> str:
    """
    Formats a datetime in ISO8601 with a trailing Z, so it's also RFC3339 for
    easier parsing by things like Golang.
    """
    return timestamp.isoformat() + "Z"


def ensure_trailing_slash(url: str) -> str:
    """
    Adds a trailing slash to `url` if not already present, and then returns it.
    """
    if url.endswith("/"):
        return url
    else:
        return url + "/"


def _die(signum: Any, frame: Any) -> None:
    print("Caught early SIGTERM. Exiting immediately!", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    # We are probably running as PID 1 so need to explicitly register a handler
    # to die on SIGTERM. This will be overwritten once we start the
    # RedisQueueWorker.
    signal.signal(signal.SIGTERM, _die)

    # Enable OpenTelemetry if the env vars are present. If this block isn't
    # run, all the opentelemetry calls are no-ops.
    if "OTEL_SERVICE_NAME" in os.environ:
        trace.set_tracer_provider(TracerProvider())
        span_processor = BatchSpanProcessor(OTLPSpanExporter())
        trace.get_tracer_provider().add_span_processor(span_processor)  # type: ignore

    config = load_config()
    predictor_ref = get_predictor_ref(config)

    parser = ArgumentParser()

    # accept positional arguments for backwards compatibility
    # TODO remove this in a future version of Cog
    parser.add_argument("positional_args", nargs="*")

    # accepting redis-host and redis-port for backwards compatibility, they are
    # replaced by the single --redis-url.
    # TODO remove these two arguments in a future version of Cog
    parser.add_argument("--redis-host")
    parser.add_argument("--redis-port", type=int)
    parser.add_argument("--redis-url")
    parser.add_argument("--input-queue")
    parser.add_argument("--s3-access-key")
    parser.add_argument("--s3-secret-key")
    parser.add_argument("--s3-endpoint-url")
    parser.add_argument("--s3-bucket")
    parser.add_argument("--s3-region")
    parser.add_argument("--consumer-id")
    parser.add_argument("--model-id")
    parser.add_argument("--predict-timeout", type=int)
    parser.add_argument("--report-setup-run-url")
    parser.add_argument(
        "--max-failure-count",
        type=int,
        help="Maximum number of consecutive failures before the worker should exit",
    )

    args = parser.parse_args()

    if len(args.positional_args) > 0:
        sys.stderr.write(
            "Positional arguments for queue worker are deprecated. Switch to flag arguments."
        )
        sys.exit(1)
    else:
        if args.redis_url is None:
            sys.stderr.write(
                "--redis-host and --redis-port arguments are deprecated. Switch to --redis-url."
            )
            args.redis_url = f"redis://{args.redis_host}:{args.redis_port}/0"
        # Configure boto3 client
        s3: ServiceResource = boto3.resource(
            "s3",
            region_name=args.s3_region,
            endpoint_url=args.s3_endpoint_url,
            aws_access_key_id=args.s3_access_key,
            aws_secret_access_key=args.s3_secret_key,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}),
        )
        worker = RedisQueueWorker(
            predictor_ref=predictor_ref,
            redis_url=args.redis_url,
            input_queue=args.input_queue,
            s3_client=s3,
            s3_bucket=args.s3_bucket,
            consumer_id=args.consumer_id,
            predict_timeout=args.predict_timeout,
            report_setup_run_url=args.report_setup_run_url,
            max_failure_count=args.max_failure_count,
        )

    workerThread = Thread(target=worker.start)
    uploadThread = Thread(target=worker.start_upload_thread)
    workerThread.start()
    uploadThread.start()
    workerThread.join()
    uploadThread.join()

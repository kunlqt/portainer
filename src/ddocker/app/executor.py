"""
"""

import docker
import functools
import json
import logging
import pesos.api
import pesos.executor
import os
import threading
import time
import re
import subprocess

from pesos.vendor.mesos import mesos_pb2

from ddocker.app import subcommand
from ddocker.proto import ddocker_pb2


logger = logging.getLogger("ddocker.executor")


@subcommand("build-executor")
def main(args):

    driver = pesos.executor.MesosExecutorDriver(Executor())

    thread = threading.Thread(target=driver.run)
    thread.setDaemon(True)
    thread.start()

    while thread.isAlive():
        time.sleep(0.5)


class Executor(pesos.api.Executor):

    TASK_STARTING = mesos_pb2.TASK_STARTING
    TASK_RUNNING = mesos_pb2.TASK_RUNNING
    TASK_FINISHED = mesos_pb2.TASK_FINISHED
    TASK_FAILED = mesos_pb2.TASK_FAILED

    def __init__(self):
        self.build_task = None

        self.docker = None
        self.docker_daemon_up = False

    def registered(self, driver, executorInfo, frameworkInfo, slaveInfo):

        logger.info("Setting up environment for building containers")

        # Parse the build task object
        try:
            build_task = ddocker_pb2.BuildTask()
            build_task.ParseFromString(executorInfo.data)
        except Exception:
            logger.error("Failed to parse BuildTask in ExecutorInfo.data")
            raise

        self.build_task = build_task

        # Launch the docker daemon
        def launch_docker_daemon():
            logger.info("Launching docker daemon subprocess")

            # TODO(tarnfeld): This should be made a little more flexible
            proc = subprocess.Popen(["/usr/local/bin/wrapdocker"])

            self.docker = docker.Client()
            while True:
                try:
                    self.docker.ping()
                except:
                    logger.info("Waiting for docker daemon to respond to pings")
                    time.sleep(1)
                else:
                    self.docker_daemon_up = True
                    break

            proc.wait()

        if not build_task.HasField("docker_host"):
            daemon_thread = threading.Thread(target=launch_docker_daemon)
            daemon_thread.setDaemon(True)
            daemon_thread.start()
        else:
            self.docker = docker.Client(build_task.docker_host)
            self.docker_daemon_up = True

    def disconnected(self, driver):
        pass

    def reregistered(self, driver, slaveInfo):
        pass

    def launch_task(self, driver, taskInfo):

        logger.info("Launched task %s", taskInfo.task_id.value)

        # Tell mesos that we're starting the task
        driver.send_status_update(mesos_pb2.TaskStatus(
            task_id=taskInfo.task_id,
            state=self.TASK_STARTING
        ))

        # Spawn another thread to run the task freeing up the executor
        thread = threading.Thread(target=functools.partial(
            self._build_image,
            driver,
            taskInfo,
            self.build_task
        ))

        thread.setDaemon(True)
        thread.start()

    def _wrap_docker_stream(self, stream):
        """Wrapper to parse the different types of messages from the
        Docker Remote API and spit them out in a friendly format."""

        for msg in stream:
            logger.info("Received update from docker: %s", msg.rstrip())

            # Parse the message / handle any errors from docker
            try:
                update = json.loads(msg.rstrip())
            except Exception, e:
                logger.error("Caught exception parsing message %s %r", msg, e)
            else:
                if "error" in update:
                    logger.error("Docker error: %s", update["error"])
                    yield update["error"]
                    raise Exception("Docker encountered an error")

                friendly_message = None

                if "stream" in update:
                    friendly_message = update["stream"].rstrip()
                if "status" in update:
                    friendly_message = update["status"].rstrip()
                    if "id" in update:
                        friendly_message = "[%s] %s" % (update["id"], friendly_message)
                    if "progress" in update:
                        friendly_message += " (%s)" % update["progress"]

                if friendly_message is not None:
                    yield friendly_message

    def _build_image(self, driver, taskInfo, buildTask):
        """Build an image for the given buildTask."""

        logger.info("Waiting for docker daemon to be available")

        # Wait for the docker daemon to be ready
        while not self.docker_daemon_up:
            time.sleep(1)

        # Now that docker is up, let's go and do stuff
        driver.send_status_update(mesos_pb2.TaskStatus(
            task_id=taskInfo.task_id,
            state=self.TASK_RUNNING
        ))

        try:
            sandbox_dir = os.environ["MESOS_DIRECTORY"]
            context_path = os.path.join(sandbox_dir, buildTask.context)

            registry_url = ""
            if buildTask.image.HasField("registry"):
                registry_url = buildTask.image.registry.hostname
                if buildTask.image.registry.HasField("port"):
                    registry_url += ":%d" % buildTask.image.registry.port
                registry_url += "/"

            image_name = "%s%s/%s" % (
                registry_url,
                buildTask.image.repository.username,
                buildTask.image.repository.repo_name
            )

            logger.info("Building image %s from context %s", image_name, context_path)

            if not os.path.exists(context_path):
                raise Exception("Context %s does not exist" % (context_path))

            with open(context_path, "r") as context:
                build_request = self.docker.build(
                    fileobj=context,
                    custom_context=True,
                    encoding="gzip",
                    stream=True
                )

                for message in self._wrap_docker_stream(build_request):
                    driver.send_framework_message(
                        str("%s: %s" % (image_name, message))
                    )

            # Extract the newly created image ID
            match = re.search(r'built (.*)$', message)
            if not match:
                raise Exception("Failed to match image ID from %r" % message)
            image_id = match.group(1)

            # Tag the image with all the required tags
            tags = buildTask.image.tag or ["latest"]
            driver.send_framework_message(str("%s: Tagging image %s" % (image_name, image_id)))
            for tag in tags:
                try:
                    self.docker.tag(
                        image=image_id,
                        repository=image_name,
                        tag=tag,
                        force=True
                    )
                    driver.send_framework_message(str("%s:  ---> %s" % (image_name, tag)))
                except Exception, e:
                    raise e

            # Push the image to the registry
            driver.send_framework_message("%s: Pushing image" % image_name)
            push_request = self.docker.push(image_name, stream=True)
            for message in self._wrap_docker_stream(push_request):
                driver.send_framework_message(
                    str("%s:  ---> %s" % (image_name, message))
                )

            driver.send_status_update(mesos_pb2.TaskStatus(
                task_id=taskInfo.task_id,
                state=self.TASK_FINISHED
            ))
        except Exception, e:
            logger.error("Caught exception building image: %s", e)
            driver.send_status_update(mesos_pb2.TaskStatus(
                task_id=taskInfo.task_id,
                state=self.TASK_FAILED
            ))

from utils.logger import SubprocessLogger
from utils.config_loader import CONFIG_MANAGER
from utils.cli_debrid_permissions import start_permission_monitor
from concurrent.futures import ThreadPoolExecutor, as_completed
import shlex, os, time, signal, threading, subprocess, sys, uvicorn
from json import dump


class ProcessHandler:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(ProcessHandler, cls).__new__(cls)
            cls._instance.init_attributes(*args, **kwargs)
            signal.signal(signal.SIGTERM, cls._instance.shutdown)
            signal.signal(signal.SIGINT, cls._instance.shutdown)
            signal.signal(signal.SIGCHLD, cls._instance.reap_zombies)
        return cls._instance

    def init_attributes(self, logger):
        self.logger = logger
        self.processes = {}
        self.process_names = {}
        self.subprocess_loggers = {}
        self.stdout = ""
        self.stderr = ""
        self.returncode = None
        self.shutting_down = False
        self.setup_tracker = set()

    def _update_running_processes_file(self):
        running_processes = {
            process_info["name"]: pid for pid, process_info in self.processes.items()
        }
        file_path = "/healthcheck/running_processes.json"
        directory = os.path.dirname(file_path)

        try:
            os.makedirs(directory, exist_ok=True)
            with open(file_path, "w") as f:
                dump(running_processes, f)
        except Exception as e:
            self.logger.error(f"Failed to write running processes file: {e}")

    def start_process(
        self,
        process_name,
        config_dir=None,
        command=None,
        instance_name=None,
        suppress_logging=False,
        env=None,
    ):
        skip_setup = {"pgAgent"}
        key = None

        if process_name in skip_setup:
            self.logger.info(
                f"{process_name} does not require setup. Skipping setup..."
            )
        else:
            key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
            if not key:
                self.logger.debug(
                    f"Failed to locate key for {process_name}. Assuming no setup required."
                )
            else:
                if process_name not in self.setup_tracker:
                    self.logger.debug(f"Pre Setup tracker: {self.setup_tracker}")
                    self.logger.info(f"{process_name} needs setup. Running setup...")
                    from utils.setup import setup_project

                    success, error = setup_project(self, process_name)
                    if not success:
                        return False, f"Failed to set up {process_name}: {error}"

        try:
            if process_name in self.process_names:
                self.logger.info(f"{process_name} is already running. Skipping...")
                return True, None

            group_id = CONFIG_MANAGER.get("pgid")
            user_id = CONFIG_MANAGER.get("puid")

            if not config_dir or not command or len(command) == 0:
                self.logger.debug(
                    f"Configuration directory or command not provided for {process_name}. Attempting to load from config..."
                )
                key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
                config = CONFIG_MANAGER.get_instance(instance_name, key)
                command = config.get("command", command)
                self.logger.debug(f"Command for {process_name}: {command}")
                config_dir = config.get("config_dir", config_dir)
                suppress_logging = config.get("suppress_logging", suppress_logging)
                env = env or {}
                env.update(config.get("env", {}))
                if config.get("wait_for_dir"):
                    dependency_dir = config["wait_for_dir"]
                    while not os.path.exists(dependency_dir):
                        self.logger.info(
                            f"Waiting for directory {dependency_dir} to become available..."
                        )
                        time.sleep(10)

            def preexec_fn():
                os.setgid(group_id)
                os.setuid(user_id)

            process_description = process_name
            self.logger.info(f"Starting {process_description} process")

            if isinstance(command, str):
                command = shlex.split(command)

            if key or instance_name:
                config = CONFIG_MANAGER.get_instance(instance_name, key)
                if key == "zurg":
                    config.get("log_level", "INFO")
                    env = config.get("env", None)
                    if env is None:
                        env = {}
                        env["LOG_LEVEL"] = config.get("log_level", "INFO")
                else:
                    env = config.get("env", None)

            process_env = os.environ.copy()
            if env is not None:
                process_env.update(env)

            rclone_instances = CONFIG_MANAGER.get("rclone", {}).get("instances", {})
            enabled_rclone_processes = [
                config.get("process_name")
                for config in rclone_instances.values()
                if config.get("enabled", False)
            ]

            process_static_list = [
                "poetry_install",
                "install_poetry",
                "poetry_env_setup",
                "PostgreSQL_init",
                "pnpm_install",
                "pnpm_build",
                "python_env_setup",
                "install_requirements",
                "setup_env_and_install",
                "dotnet_env_restore",
                "dotnet_publish",
            ]

            if enabled_rclone_processes:
                process_static_list.extend(enabled_rclone_processes)

            skip_preexec = process_name in process_static_list

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                cwd=config_dir,
                universal_newlines=True,
                bufsize=1,
                preexec_fn=(preexec_fn if not skip_preexec else None),
                env=process_env,
            )

            if not suppress_logging:
                subprocess_logger = SubprocessLogger(
                    self.logger, f"{process_description}"
                )
                subprocess_logger.start_logging_stdout(process)
                subprocess_logger.start_monitoring_stderr(
                    process, instance_name, process_name
                )
                self.subprocess_loggers[process_name] = subprocess_logger

            # success, error = self._check_immediate_exit_and_log(process, process_name)
            # if not success:
            #    return False, error

            self.logger.info(f"{process_name} process started with PID: {process.pid}")

            if any("plexmediaserver" in part for part in command):
                self.logger.info(
                    "If you see 'Critical: libusb_init failed' in the logs, "
                    "it is a known issue with Plex and can be ignored."
                )

            self.processes[process.pid] = {
                "name": process_name,
                "description": process_description,
                "process_obj": process,
            }
            self.process_names[process_name] = process

            if process_name == "CLI Debrid":
                threading.Thread(target=start_permission_monitor, daemon=True).start()
                self.logger.info("Started CLI Debrid permission fix monitor")

            if process:
                self._update_running_processes_file()
            return True, None

        except Exception as e:
            return False, f"Error running subprocess for {process_name}: {e}"

    def _check_immediate_exit_and_log(self, process, process_name):
        time.sleep(0.5)
        if process.poll() is not None:
            stdout_output = process.stdout.read().strip()
            stderr_output = process.stderr.read().strip()

            self.logger.error(
                f"{process_name} exited immediately with return code {process.returncode}"
            )
            if stdout_output:
                self.logger.error(f"{process_name} stdout:\n{stdout_output}")
            if stderr_output:
                self.logger.error(f"{process_name} stderr:\n{stderr_output}")
            return False, f"{process_name} failed to start. See logs for details."

        return True, None

    def reap_zombies(self, signum, frame):
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
                process_info = self.processes.pop(pid, {"description": "Unknown"})
                process_name = process_info.get("name")
                if process_name in self.process_names:
                    del self.process_names[process_name]
                self.logger.debug(
                    f"Reaped zombie process with PID: {pid}, "
                    f"Description: {process_info.get('description', 'Unknown')}"
                )
            except ChildProcessError:
                break

    def wait(self, process_name):
        if self.shutting_down:
            self.logger.debug(f"Skipping wait for {process_name} due to shutdown mode.")
            return

        process = self.process_names.get(process_name)

        if not process:
            self.logger.warning(
                f"Process {process_name} is not running or has already exited."
            )
            return

        try:
            process.wait()
            self.returncode = process.returncode
            if process.stdout:
                self.stdout = process.stdout.read().strip()
            if process.stderr:
                self.stderr = process.stderr.read().strip()
        except Exception as e:
            self.logger.error(f"Error while waiting for process {process_name}: {e}")
        finally:
            if process_name in self.subprocess_loggers:
                self.subprocess_loggers[process_name].stop_logging_stdout()
                self.subprocess_loggers[process_name].stop_monitoring_stderr()
                del self.subprocess_loggers[process_name]

            if process.pid in self.processes:
                del self.processes[process.pid]

            if process_name in self.process_names:
                del self.process_names[process_name]

    def stop_process(self, process_name):
        try:
            process_description = process_name
            self.logger.info(f"Initiating shutdown for {process_description}")

            process = self.process_names.get(process_name)
            if process:
                self.logger.debug(f"Process {process_name} found: {process}")
                process.terminate()
                max_attempts = 1 if process_name == "riven_backend" else 3
                attempt = 0
                while attempt < max_attempts:
                    self.logger.debug(
                        f"Waiting for {process_description} to terminate (attempt {attempt + 1})..."
                    )
                    try:
                        process.wait(timeout=10)
                        if process.poll() is not None:
                            self.logger.info(
                                f"{process_description} process terminated gracefully."
                            )
                            break
                    except subprocess.TimeoutExpired:
                        self.logger.warning(
                            f"{process_description} process did not terminate within 10 seconds on attempt {attempt + 1}."
                        )
                    attempt += 1
                    time.sleep(5)
                if process.poll() is None:
                    self.logger.warning(
                        f"{process_description} process did not terminate, forcing shutdown."
                    )
                    process.kill()
                    process.wait()
                    self.logger.info(
                        f"{process_description} process forcefully terminated."
                    )
                if self.subprocess_loggers.get(process_name):
                    self.subprocess_loggers[process_name].stop_logging_stdout()
                    self.subprocess_loggers[process_name].stop_monitoring_stderr()
                    del self.subprocess_loggers[process_name]
                    self.logger.debug(f"Stopped logging for {process_description}")
                self.process_names.pop(process_name, None)
                process_info = self.processes.pop(process.pid, None)
                if process_info:
                    self.logger.debug(
                        f"Removed {process_description} with PID {process.pid} from tracking."
                    )
                self.logger.info(f"{process_description} shutdown completed.")
                self._update_running_processes_file()
            else:
                self.logger.warning(
                    f"{process_description} was not found or has already been stopped."
                )
        except Exception as e:
            self.logger.error(
                f"Error occurred while stopping {process_description}: {e}"
            )

    def shutdown_threads(self, *args, **kwargs):
        self.logger.debug(
            f"shutdown_threads called with args: {args}, kwargs: {kwargs}"
        )
        for thread in threading.enumerate():
            if thread.is_alive() and thread is not threading.main_thread():
                self.logger.info(f"Joining thread: {thread.name}")
                thread.join(timeout=5)
                if thread.is_alive():
                    self.logger.warning(
                        f"Thread {thread.name} did not terminate in time."
                    )

    def shutdown(self, signum=None, frame=None, exit_code=0):
        self.shutting_down = True
        self.logger.info("Shutdown signal received. Cleaning up...")
        processes_to_stop = list(self.process_names.keys())
        self.logger.info(f"Processes to stop: {', '.join(processes_to_stop)}")

        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(self.stop_process, process_name): process_name
                for process_name in processes_to_stop
                if process_name in self.process_names
            }

            for future in as_completed(futures):
                process_name = futures[future]
                try:
                    future.result()
                    self.logger.info(f"{process_name} has been stopped successfully.")
                except Exception as e:
                    self.logger.error(f"Error stopping {process_name}: {e}")
        self._update_running_processes_file()
        self.shutdown_threads()
        time.sleep(5)
        self.unmount_all()
        uvicorn.Server.should_exit = True
        self.logger.info("Shutdown complete.")
        sys.exit(exit_code)

    def unmount_all(self):
        rclone_instances = CONFIG_MANAGER.get("rclone", {}).get("instances", {})
        for instance_name, instance_config in rclone_instances.items():
            if instance_config.get("enabled", False):
                rclone_dir = instance_config.get("mount_dir")
                rclone_mount_name = instance_config.get("mount_name")
                rclone_mount_path = os.path.join(rclone_dir, rclone_mount_name)
                if os.path.ismount(rclone_mount_path):
                    self.logger.info(
                        f"Unmounting rclone mount for instance {instance_name} at {rclone_mount_path}..."
                    )
                    umount = subprocess.run(
                        ["umount", rclone_mount_path], capture_output=True, text=True
                    )
                    if umount.returncode == 0:
                        self.logger.info(
                            f"Successfully unmounted rclone mount for instance {instance_name}: {rclone_mount_path}"
                        )
                    else:
                        self.logger.error(
                            f"Failed to unmount rclone mount for instance {instance_name}: {rclone_mount_path}: {umount.stderr.strip()}"
                        )

from utils.logger import (
    SubprocessLogger,
    get_subprocess_file_logger,
    get_subprocess_access_logger,
)
from utils.config_loader import CONFIG_MANAGER
from concurrent.futures import ThreadPoolExecutor, as_completed
import shlex, os, time, signal, threading, subprocess, sys, uvicorn, socket, psutil
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
        self.external_processes = {}
        self.subprocess_loggers = {}
        self.stdout = ""
        self.stderr = ""
        self.returncode = None
        self.shutting_down = False
        self.setup_tracker = set()
        self.auto_restart_state = {}
        self.auto_restart_lock = threading.Lock()
        self.auto_restart_thread = None

    def _update_running_processes_file(self):
        running_processes = {
            process_info["name"]: pid for pid, process_info in self.processes.items()
        }
        if self.external_processes:
            running_processes.update(self.external_processes)
        file_path = "/healthcheck/running_processes.json"
        directory = os.path.dirname(file_path)

        try:
            os.makedirs(directory, exist_ok=True)
            with open(file_path, "w") as f:
                dump(running_processes, f)
        except Exception as e:
            self.logger.error(f"Failed to write running processes file: {e}")

    def register_external_process(self, process_name, pid):
        if not process_name or not pid:
            return
        self.external_processes[process_name] = pid
        self._update_running_processes_file()

    def unregister_external_process(self, process_name):
        if process_name in self.external_processes:
            del self.external_processes[process_name]
            self._update_running_processes_file()

    def start_process(
        self,
        process_name,
        config_dir=None,
        command=None,
        instance_name=None,
        suppress_logging=False,
        env=None,
    ):
        self._set_restart_disabled(process_name, False)
        self._reset_healthcheck_state(process_name)
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
                if key == "emby":
                    try:
                        from utils.emby_settings import patch_emby_config

                        patch_emby_config(config.get("port"))
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to patch Emby system.xml port: {e}"
                        )
                if key == "jellyfin":
                    try:
                        from utils.jellyfin_settings import patch_jellyfin_config

                        patch_jellyfin_config(config.get("port"))
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to patch Jellyfin system.xml port: {e}"
                        )

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
                "poetry_update_plexapi",
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
                "go_build",
                "Plex DBRepair",
                "dbrepair",
            ]

            if enabled_rclone_processes:
                process_static_list.extend(enabled_rclone_processes)

            skip_preexec = process_name in process_static_list

            stdout_target = subprocess.DEVNULL if suppress_logging else subprocess.PIPE
            stderr_target = subprocess.DEVNULL if suppress_logging else subprocess.PIPE

            subprocess_file_logger = None
            subprocess_access_logger = None
            if not suppress_logging and key in {
                "nzbdav",
                "zilean",
                "rclone",
                "traefik",
                "dumb_frontend",
                "phalanx_db",
                "postgres",
            }:
                log_file = config.get("log_file")
                if log_file:
                    subprocess_file_logger = get_subprocess_file_logger(
                        log_file,
                        log_level=config.get("log_level", "INFO"),
                        log_name=f"{process_name}-subprocess",
                    )
                if key == "traefik":
                    access_log_file = config.get("access_log_file")
                    if access_log_file:
                        subprocess_access_logger = get_subprocess_access_logger(
                            access_log_file,
                            log_name=f"{process_name}-access",
                        )
                if key == "rclone" and isinstance(command, list):
                    filtered_command = []
                    skip_next = False
                    for part in command:
                        if skip_next:
                            skip_next = False
                            continue
                        if part == "--log-file":
                            skip_next = True
                            continue
                        if part.startswith("--log-file="):
                            continue
                        filtered_command.append(part)
                    command = filtered_command

            process = subprocess.Popen(
                command,
                stdout=stdout_target,
                stderr=stderr_target,
                start_new_session=True,
                cwd=config_dir,
                universal_newlines=True,
                bufsize=1,
                preexec_fn=(preexec_fn if not skip_preexec else None),
                env=process_env,
            )

            if not suppress_logging:
                subprocess_logger = SubprocessLogger(
                    self.logger,
                    f"{process_description}",
                    file_logger=subprocess_file_logger,
                    access_logger=subprocess_access_logger,
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

            if (
                isinstance(command, list)
                and command
                and "plexmediaserver" in command[0]
            ):
                self.logger.info(
                    "If you see 'Critical: libusb_init failed' in the logs, "
                    "it is a known issue with Plex and can be ignored."
                )

            self.processes[process.pid] = {
                "name": process_name,
                "description": process_description,
                "process_obj": process,
                "start_time": time.time(),
            }
            self.process_names[process_name] = process

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
                process_obj = process_info.get("process_obj")
                exit_code = None
                if process_obj:
                    exit_code = process_obj.returncode
                if process_name in self.process_names:
                    del self.process_names[process_name]
                self.logger.debug(
                    f"Reaped zombie process with PID: {pid}, "
                    f"Description: {process_info.get('description', 'Unknown')}"
                )
                if process_name:
                    reason = (
                        f"Exited with code {exit_code}"
                        if exit_code is not None
                        else "Exited"
                    )
                    self._maybe_schedule_restart(process_name, reason)
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

            self._update_running_processes_file()

    def stop_process(self, process_name, disable_restart=True):
        try:
            process_description = process_name
            self.logger.info(f"Initiating shutdown for {process_description}")

            process = self.process_names.get(process_name)
            if process:
                self.logger.debug(f"Process {process_name} found: {process}")
                if disable_restart:
                    self._set_restart_disabled(process_name, True)
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

    def start_auto_restart_monitor(self):
        if self.auto_restart_thread and self.auto_restart_thread.is_alive():
            return

        def monitor():
            while not self.shutting_down:
                cfg = self._get_auto_restart_config()
                if not cfg.get("enabled", False):
                    time.sleep(5)
                    continue
                grace_period = cfg.get("grace_period_seconds", 30)
                for process_name, process in list(self.process_names.items()):
                    if self.shutting_down:
                        break
                    policy = self._get_service_restart_policy(process_name)
                    if not policy:
                        continue
                    if not policy.get("restart_on_unhealthy", True):
                        continue
                    if self._is_restart_disabled(process_name):
                        continue
                    if not process or process.poll() is not None:
                        continue
                    if not self._is_ready_for_healthcheck(process_name, grace_period):
                        continue
                    if not self._is_healthcheck_due(process_name, policy):
                        continue
                    healthy, reason = self._check_process_health(
                        process_name, process.pid
                    )
                    should_restart = self._record_healthcheck_result(
                        process_name, healthy, reason, policy
                    )
                    self._set_last_healthcheck_time(process_name)
                    if should_restart and reason:
                        self._maybe_schedule_restart(process_name, reason)
                time.sleep(5)

        self.auto_restart_thread = threading.Thread(
            target=monitor, daemon=True, name="auto-restart-monitor"
        )
        self.auto_restart_thread.start()

    def _get_auto_restart_config(self):
        cfg = CONFIG_MANAGER.get("dumb", {}).get("auto_restart", {}) or {}
        defaults = {
            "enabled": False,
            "restart_on_unhealthy": True,
            "healthcheck_interval": 30,
            "unhealthy_threshold": 3,
            "max_restarts": 3,
            "window_seconds": 300,
            "backoff_seconds": [5, 15, 45, 120],
            "grace_period_seconds": 30,
            "services": [],
        }
        merged = defaults.copy()
        merged.update(cfg)
        return merged

    def _get_restart_state(self, process_name):
        return self.auto_restart_state.setdefault(
            process_name,
            {
                "restart_attempts": 0,
                "restart_successes": 0,
                "restart_failures": 0,
                "recent_attempts": [],
                "pending": False,
                "next_restart_time": None,
                "disabled": False,
                "last_restart_time": None,
                "last_failure_reason": None,
                "last_exit_time": None,
                "last_exit_reason": None,
                "last_healthcheck_time": None,
                "unhealthy_count": 0,
            },
        )

    def _normalize_restart_process_name(self, name):
        return (name or "").strip().lower()

    def _get_service_restart_policy(self, process_name):
        cfg = self._get_auto_restart_config()
        if not cfg.get("enabled", False):
            return None

        services = cfg.get("services", [])
        if not services:
            return None

        target = self._normalize_restart_process_name(process_name)
        for entry in services:
            if not isinstance(entry, dict):
                continue
            entry_name = self._normalize_restart_process_name(entry.get("process_name"))
            if not entry_name or entry_name != target:
                continue
            merged = cfg.copy()
            merged.update(entry)
            return merged if merged.get("enabled", cfg.get("enabled", False)) else None
        return None

    def _is_healthcheck_due(self, process_name, policy):
        interval = policy.get("healthcheck_interval", 30)
        if interval <= 0:
            return False
        with self.auto_restart_lock:
            state = self._get_restart_state(process_name)
            last = state.get("last_healthcheck_time")
        if not last:
            return True
        return (time.time() - last) >= interval

    def _set_last_healthcheck_time(self, process_name):
        with self.auto_restart_lock:
            state = self._get_restart_state(process_name)
            state["last_healthcheck_time"] = time.time()

    def _record_healthcheck_result(self, process_name, healthy, reason, policy):
        if healthy:
            with self.auto_restart_lock:
                state = self._get_restart_state(process_name)
                state["unhealthy_count"] = 0
            return False

        threshold = policy.get("unhealthy_threshold", 3)
        with self.auto_restart_lock:
            state = self._get_restart_state(process_name)
            state["unhealthy_count"] += 1
            count = state["unhealthy_count"]
            if count >= threshold:
                state["unhealthy_count"] = 0
                return True
        return False

    def _reset_healthcheck_state(self, process_name):
        with self.auto_restart_lock:
            state = self._get_restart_state(process_name)
            state["unhealthy_count"] = 0
            state["last_healthcheck_time"] = None

    def _set_restart_disabled(self, process_name, disabled):
        with self.auto_restart_lock:
            state = self._get_restart_state(process_name)
            state["disabled"] = disabled

    def _is_restart_disabled(self, process_name):
        with self.auto_restart_lock:
            state = self._get_restart_state(process_name)
            return state.get("disabled", False)

    def _is_ready_for_healthcheck(self, process_name, grace_period):
        for pid, info in self.processes.items():
            if info.get("name") == process_name:
                start_time = info.get("start_time")
                if not start_time:
                    return True
                return (time.time() - start_time) >= grace_period
        return True

    def _maybe_schedule_restart(self, process_name, reason):
        if self.shutting_down or self._is_restart_disabled(process_name):
            return
        policy = self._get_service_restart_policy(process_name)
        if not policy:
            return

        now = time.time()
        with self.auto_restart_lock:
            state = self._get_restart_state(process_name)
            window_seconds = policy.get("window_seconds", 300)
            recent = [t for t in state["recent_attempts"] if now - t < window_seconds]
            state["recent_attempts"] = recent
            if len(recent) >= policy.get("max_restarts", 3):
                state["last_failure_reason"] = reason
                self.logger.warning(
                    f"Auto-restart suppressed for {process_name}: restart limit reached."
                )
                return
            if state["pending"]:
                return

            backoffs = policy.get("backoff_seconds", [5, 15, 45, 120])
            index = min(len(recent), max(len(backoffs) - 1, 0))
            delay = backoffs[index] if backoffs else 0
            state["pending"] = True
            state["next_restart_time"] = now + delay
            state["last_exit_time"] = now
            state["last_exit_reason"] = reason

        def do_restart():
            if delay:
                time.sleep(delay)
            if self.shutting_down or self._is_restart_disabled(process_name):
                with self.auto_restart_lock:
                    state = self._get_restart_state(process_name)
                    state["pending"] = False
                    state["next_restart_time"] = None
                return
            if process_name in self.process_names:
                self.stop_process(process_name, disable_restart=False)

            with self.auto_restart_lock:
                state = self._get_restart_state(process_name)
                state["restart_attempts"] += 1
                state["recent_attempts"].append(time.time())

            success, error = self.start_process(process_name)
            with self.auto_restart_lock:
                state = self._get_restart_state(process_name)
                state["pending"] = False
                state["next_restart_time"] = None
                if success:
                    now_ts = time.time()
                    state["restart_successes"] += 1
                    state["last_restart_time"] = now_ts
                    state["last_failure_reason"] = None
                else:
                    state["restart_failures"] += 1
                    state["last_failure_reason"] = error
            if success:
                self.logger.warning(
                    f"Auto-restarted {process_name} after failure: {reason}"
                )
            else:
                self.logger.error(f"Auto-restart failed for {process_name}: {error}")

        threading.Thread(
            target=do_restart, daemon=True, name=f"auto-restart-{process_name}"
        ).start()

    def _collect_config_ports(self, config):
        ports = set()
        for key in ("port", "frontend_port", "backend_port", "webdav_port"):
            value = config.get(key)
            if isinstance(value, int):
                ports.add(value)
        env = config.get("env", {})
        for key in ("PORT", "FRONTEND_PORT", "BACKEND_PORT", "WEBDAV_PORT"):
            value = env.get(key)
            if isinstance(value, str) and value.isdigit():
                ports.add(int(value))
        return sorted(ports)

    def _normalize_host(self, host):
        if not host or host in {"0.0.0.0", "::"}:
            return "127.0.0.1"
        return host

    def _is_port_open(self, host, port, timeout=1.5):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _get_process_config(self, process_name):
        if not CONFIG_MANAGER:
            return None
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        if not key and not instance_name:
            return None
        return CONFIG_MANAGER.get_instance(instance_name, key)

    def _check_process_health(self, process_name, pid):
        if not pid or not psutil.pid_exists(pid):
            return False, "Process PID not running"

        try:
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return False, "Process not healthy"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False, "Process could not be inspected"

        config = self._get_process_config(process_name)
        if not config:
            return True, None

        host = self._normalize_host(config.get("host"))
        ports = self._collect_config_ports(config)
        for port in ports:
            if not self._is_port_open(host, port):
                return False, f"Port {host}:{port} not responding"

        return True, None

    def get_restart_stats(self, process_name):
        policy = self._get_service_restart_policy(process_name)
        if policy:
            unhealthy_threshold = policy.get("unhealthy_threshold", 3)
        else:
            unhealthy_threshold = self._get_auto_restart_config().get(
                "unhealthy_threshold", 3
            )
        with self.auto_restart_lock:
            state = self._get_restart_state(process_name)
            return {
                "restart_attempts": state["restart_attempts"],
                "restart_successes": state["restart_successes"],
                "restart_failures": state["restart_failures"],
                "recent_restart_attempts": len(state["recent_attempts"]),
                "pending": state["pending"],
                "next_restart_time": state["next_restart_time"],
                "disabled": state["disabled"],
                "last_restart_time": state["last_restart_time"],
                "last_failure_reason": state["last_failure_reason"],
                "last_exit_time": state["last_exit_time"],
                "last_exit_reason": state["last_exit_reason"],
                "unhealthy_count": state["unhealthy_count"],
                "unhealthy_threshold": unhealthy_threshold,
            }

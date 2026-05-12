"""Install systemd services and timers from configs.json schedule settings."""

from __future__ import annotations

import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import utilities as utils

PROJECT_DIR = "/home/ubuntu/index_pcr"
BACKEND_DIR = f"{PROJECT_DIR}/backend"
PYTHON = f"{PROJECT_DIR}/.venv/bin/python"

API_SERVICE = "index-pcr-api.service"
API_STOP_SERVICE = "index-pcr-api-stop.service"
API_START_TIMER = "index-pcr-api-start.timer"
API_STOP_TIMER = "index-pcr-api-stop.timer"
WORKER_SERVICE = "index-pcr-worker.service"
WORKER_STOP_SERVICE = "index-pcr-worker-stop.service"
WORKER_START_TIMER = "index-pcr-worker-start.timer"
WORKER_STOP_TIMER = "index-pcr-worker-stop.timer"
DAILY_RESTART_SERVICE = "index-pcr-daily-restart.service"
DAILY_RESTART_TIMER = "index-pcr-daily-restart.timer"


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True)


def schedule_value(key: str) -> str:
    value = str(utils.schedule_config()[key])
    utils.parse_hhmm(value)
    return value


def calendar_at(time_key: str) -> str:
    return f"{utils.schedule_config()['weekdays']} *-*-* {schedule_value(time_key)}:00"


def install_unit(name: str, content: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        run("sudo", "-n", "cp", tmp_path, f"/etc/systemd/system/{name}")
        run("sudo", "-n", "chmod", "644", f"/etc/systemd/system/{name}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def service_unit(
    description: str,
    command: str,
    *,
    restart: str,
    install: bool = False,
) -> str:
    unit = f"""[Unit]
Description={description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory={BACKEND_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart={command}
Restart={restart}
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal
"""
    if install:
        unit += """
[Install]
WantedBy=multi-user.target
"""
    return unit


def stop_unit(description: str, service: str) -> str:
    return f"""[Unit]
Description={description}

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl stop {service}
"""


def daily_restart_unit() -> str:
    return f"""[Unit]
Description=Restart Index PCR services before market open

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart {API_SERVICE}
ExecStart=/usr/bin/systemctl restart {WORKER_SERVICE}
"""


def timer_unit(description: str, calendar: str, unit: str) -> str:
    return f"""[Unit]
Description={description}

[Timer]
OnCalendar={calendar}
Unit={unit}
Persistent=true

[Install]
WantedBy=timers.target
"""


def current_time_in_window(start_key: str, stop_key: str) -> bool:
    now = utils.now_ist()
    if now.weekday() >= 5:
        return False
    start = utils.parse_hhmm(schedule_value(start_key))
    stop = utils.parse_hhmm(schedule_value(stop_key))
    return start <= now.time() < stop


def install() -> None:
    utils.ensure_backend_config()

    install_unit(
        API_SERVICE,
        service_unit(
            "Index PCR FastAPI Backend",
            f"{PYTHON} {BACKEND_DIR}/api.py",
            restart="always",
            install=True,
        ),
    )
    install_unit(
        WORKER_SERVICE,
        service_unit(
            "Index PCR Market Data Worker",
            f"{PYTHON} {BACKEND_DIR}/main.py",
            restart="on-failure",
        ),
    )
    install_unit(
        WORKER_STOP_SERVICE,
        stop_unit("Stop Index PCR Market Data Worker", WORKER_SERVICE),
    )
    install_unit(
        DAILY_RESTART_SERVICE,
        daily_restart_unit(),
    )
    install_unit(
        WORKER_START_TIMER,
        timer_unit(
            "Start Index PCR Market Data Worker from configs.json",
            calendar_at("worker_start_time"),
            WORKER_SERVICE,
        ),
    )
    install_unit(
        WORKER_STOP_TIMER,
        timer_unit(
            "Stop Index PCR Market Data Worker from configs.json",
            calendar_at("worker_stop_time"),
            WORKER_STOP_SERVICE,
        ),
    )
    install_unit(
        DAILY_RESTART_TIMER,
        timer_unit(
            "Restart Index PCR services from configs.json",
            calendar_at("daily_restart_time"),
            DAILY_RESTART_SERVICE,
        ),
    )

    run("sudo", "-n", "systemctl", "daemon-reload")
    for old_unit in (
        "index-pcr.service",
        "index-pcr-worker.timer",
        API_START_TIMER,
        API_STOP_TIMER,
    ):
        run("sudo", "-n", "systemctl", "disable", "--now", old_unit, check=False)
    run("sudo", "-n", "systemctl", "enable", "--now", API_SERVICE)
    run("sudo", "-n", "systemctl", "disable", WORKER_SERVICE, check=False)
    for timer in (
        DAILY_RESTART_TIMER,
        WORKER_START_TIMER,
        WORKER_STOP_TIMER,
    ):
        run("sudo", "-n", "systemctl", "enable", "--now", timer)

    worker_action = (
        "start" if current_time_in_window("worker_start_time", "worker_stop_time") else "stop"
    )
    run("sudo", "-n", "systemctl", worker_action, WORKER_SERVICE, check=False)

    print(f"Installed scheduler at {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    install()

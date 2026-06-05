import subprocess
import sys
import time

ProcessEntry = tuple[str, subprocess.Popen[bytes]]

COMMANDS: tuple[tuple[str, list[str]], ...] = (
    (
        "app",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--reload",
        ],
    ),
    (
        "worker",
        [
            sys.executable,
            "-m",
            "app.worker",
        ],
    ),
)


def start_process(name: str, command: list[str]) -> ProcessEntry:
    print(f"Starting {name}: {' '.join(command)}", flush=True)
    return name, subprocess.Popen(command)


def stop_processes(processes: list[ProcessEntry]) -> None:
    for name, process in processes:
        if process.poll() is None:
            print(f"Stopping {name}", flush=True)
            process.terminate()

    for _, process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def main() -> int:
    processes = [start_process(name, command) for name, command in COMMANDS]

    try:
        while True:
            for name, process in processes:
                exit_code = process.poll()
                if exit_code is not None:
                    print(f"{name} exited with code {exit_code}", flush=True)
                    return exit_code

            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down dev processes", flush=True)
        return 0
    finally:
        stop_processes(processes)


if __name__ == "__main__":
    raise SystemExit(main())

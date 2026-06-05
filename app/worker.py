import time
from app.runs import get_active_runs, fail_run
from app.orchestrator import advance_run_through_local_steps

POLL_INTERVAL_SECONDS = 5


def main() -> None:
    while True:
        runs = get_active_runs()
        for run in runs:
            try:
                advance_run_through_local_steps(run)
            except Exception as e:
                fail_run(str(run["id"]), str(e))
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
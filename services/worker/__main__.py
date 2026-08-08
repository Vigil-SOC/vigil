import asyncio

from arq.worker import run_worker

from services.worker.jobs import WorkerSettings


def main():
    # Python 3.12+ dropped implicit loop creation; ARQ's Worker needs one to exist.
    asyncio.set_event_loop(asyncio.new_event_loop())
    run_worker(WorkerSettings)


if __name__ == "__main__":
    main()

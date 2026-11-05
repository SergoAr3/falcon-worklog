"""Add a few fictional work sessions to a fresh database."""

from datetime import datetime, timezone
from pathlib import Path
import os

from app import TimeTrackerStore


class DemoClock:
    def __init__(self, timestamp):
        self.timestamp = timestamp

    def __call__(self):
        return self.timestamp


def main():
    path = os.environ.get('TIME_TRACKER_DB', str(Path(__file__).resolve().parents[1] / 'data' / 'tracker.sqlite3'))
    clock = DemoClock(int(datetime.now(timezone.utc).timestamp()) - 16000)
    store = TimeTrackerStore(path, clock=clock)
    if store.employees():
        print('Demo data can only be added to an empty database.')
        return

    sessions = [
        ('Alex Morgan', [('Project planning', 3600), ('Client dashboard', 2500)]),
        ('Sam Rivera', [('Quality assurance', 1800), ('Sprint documentation', 3300)]),
        ('Jordan Lee', [('Support requests', 2100)]),
    ]
    for name, entries in sessions:
        employee = store.add_employee(name)
        for description, seconds in entries:
            store.start(employee['id'], description)
            clock.timestamp += seconds
            store.stop(employee['id'])
            clock.timestamp += 40
    print(f'Added {len(sessions)} fictional employees to {path}')


if __name__ == '__main__':
    main()

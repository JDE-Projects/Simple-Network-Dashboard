# Empty on purpose: its presence at the repo root makes pytest add the repo
# root to sys.path, so `from main import ...` in tests/ resolves without
# relying on the working directory pytest happened to be launched from.

# Tiny Tasks

Tiny Tasks is a small to-do list, and Footfall's demo app. You can add tasks with due dates, search them, mark them done, delete them and sign out. It is written in standard-library Python and vanilla JavaScript, stores its data in SQLite, and has no dependencies to install.

It has twelve usability and accessibility bugs, planted on purpose, and [BUGS.md](BUGS.md) lists each one, where it lives and which persona should find it. Footfall's testers are measured against that list, so please do not fix them. Two tests pin the search and empty-task bugs to stop that happening by accident.

Everything else is built the way it should be, so the code is safe to copy from.

## Run it locally

You need Python 3.12 or later. From this folder:

```bash
FOOTFALL_USERNAME=demo FOOTFALL_PASSWORD='choose-something' python3 seed.py
python3 serve.py
```

Then open http://127.0.0.1:8080 and sign in with that username and password. `seed.py` creates the account and a dozen tasks, and running it again resets them. It reads the password from the environment and never prints it.

`serve.py` listens on 127.0.0.1, so nobody else on your network can reach it. It takes its port from `PORT` and its database file from `TINY_TASKS_DB`, which defaults to `tiny-tasks.sqlite3` in this folder. `--host 0.0.0.0` listens on every interface, which the Footfall container needs and a local run does not.

The session cookie has no `Secure` flag only because Footfall's sandbox serves plain http. A real deployment behind https should set it.

## Run Footfall against it

`footfall.toml` is the app's manifest, and `footfall/briefs/default.md` is the brief the testers read. Footfall makes a username and password for each run and passes them to `seed.py`, as it does for any app with its own sign-in.

The testers are adapted from [mobile-agent-testers](https://github.com/pierridotite/mobile-agent-testers), an MIT-licensed project that runs AI testers against native mobile apps.

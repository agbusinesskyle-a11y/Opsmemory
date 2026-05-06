"""OpsMemory notifications package.

Sub-modules:
  schedule.py    Schedule shape contract + validator (single source of
                 truth shared by the API body validator and the
                 scheduler so they can never disagree).
  digest.py      Pure payload builder. Takes a user, a pref, and a
                 tasks list; returns the dict the sender will encrypt
                 and ship via Web Push / Slack DM / email.
"""

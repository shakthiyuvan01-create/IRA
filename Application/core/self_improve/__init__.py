"""Self Improve: IRA changes its own code from a plain-language request.

    agent.py   plans the change with the coding model, validates it, builds a diff
    journal.py applies an approved change, backs up every touched file, logs it,
               and can undo it
    panel.py   the Self Improve chat panel that sits beside the Activity Log

Nothing is written to disk until the person presses Apply on the diff.
"""

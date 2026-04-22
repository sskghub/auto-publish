"""Sanity test: the modal stub in conftest.py lets us import the main app."""


def test_import_autopublish_app():
    import autopublish_app  # noqa: F401

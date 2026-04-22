"""Pytest fixtures for Gecko custom component tests."""

# Pytest requires this exact name at module scope; pylint wants UPPER_CASE for "constants".
pytest_plugins = "pytest_homeassistant_custom_component"  # pylint: disable=invalid-name

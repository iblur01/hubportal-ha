"""Configuration commune du harnais Home Assistant."""

import sys
from pathlib import Path

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"
sys.path.insert(0, str(Path(__file__).parents[1]))


@pytest.fixture(autouse=True)
def enable_custom_components(enable_custom_integrations):
    """Autorise les composants personnalisés pendant les tests."""
    return


@pytest.fixture(autouse=True)
async def unload_entries_after_test(hass):
    """Arrête les coordinators et leurs horloges entre les tests."""
    yield
    for entry in hass.config_entries.async_entries("hubportal"):
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def custom_components_path():
    return Path(__file__).parents[1] / "custom_components"

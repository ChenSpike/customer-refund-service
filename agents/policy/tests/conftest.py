def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: destructive Azure/GCP integration test requiring explicit opt-in",
    )

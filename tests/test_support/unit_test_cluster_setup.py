

def registration_data(**changes):
    return {"name": "New science cluster", "host": "fixture.invalid", "user": "fixtureuser",
        "port": 22, "remote_root": "/fixture/jobs/new-science", **changes}

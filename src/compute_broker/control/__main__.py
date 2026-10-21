import os


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "compute_broker.control.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(["compute-broker", "runserver", "--noreload", f"{os.environ.get('BROKER_HOST', '127.0.0.1')}:{os.environ.get('BROKER_PORT', '8765')}"])


if __name__ == "__main__":
    main()

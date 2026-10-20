import os
import subprocess
import sys
import venv

VENV_DIR = "venv"


def create_virtual_environment():
    if not os.path.exists(VENV_DIR):
        print(f"Creating virtual environment in '{VENV_DIR}'...")
        venv.create(VENV_DIR, with_pip=True)
        print("✅ Virtual environment created.")
    else:
        print(f"Virtual environment '{VENV_DIR}' already exists.")


def get_venv_python():
    if sys.platform == "win32":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    else:
        return os.path.join(VENV_DIR, "bin", "python")


def install_requirements(python_exec):
    print("🔧 Installing dependencies from requirements.txt...")

    if not os.path.exists("requirements.txt"):
        print("❌ requirements.txt not found.")
        return

    try:
        # Upgrade pip
        subprocess.run([python_exec, "-m", "pip", "install", "--upgrade", "pip"], check=True)

        # Install from requirements.txt
        subprocess.run(
            [python_exec, "-m", "pip", "install", "--no-cache-dir", "-r", "requirements.txt"],
            check=True,
        )

        print("✅ All dependencies installed successfully.")

    except subprocess.CalledProcessError as e:
        print("❌ An error occurred during installation.")
        print("Details:", e)


def main():
    create_virtual_environment()
    python_exec = get_venv_python()
    install_requirements(python_exec)


if __name__ == "__main__":
    main()

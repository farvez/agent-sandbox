"""Lesson 1: where does this code run?"""
import os
import platform
import socket

print("Hello from", socket.gethostname())
print("Python", platform.python_version(), "on", platform.system(), platform.machine())
print("Working directory:", os.getcwd())
print("Running as user id", os.getuid())
print("Files here:", sorted(os.listdir(".")))

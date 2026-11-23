#!/bin/bash
# Runs the code that is in THIS repo. The old version cloned the upstream
# project here, which threw away every change made in this repo.
cd "$(dirname "$0")"
echo ">> STARTING MUSIC PLAYER USERBOT..."
exec python3 main.py

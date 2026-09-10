#!/usr/bin/env bash
# Runs ONCE on a fresh Ubuntu server (as the ubuntu user): installs Docker, makes the folders.
set -euo pipefail
sudo apt-get update -q
sudo apt-get install -y -q ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update -q
sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo mkdir -p /srv/closeout/app /srv/closeout/data
sudo chown -R ubuntu:ubuntu /srv/closeout
echo "server ready"

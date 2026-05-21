#!/bin/bash
set -e

echo "==> Updating system..."
sudo apt update && sudo apt upgrade -y

echo "==> Installing Python 3.10+..."
sudo apt install -y python3 python3-pip python3-venv git

echo "==> Creating bot directory..."
mkdir -p ~/telegram_bot
cd ~/telegram_bot

echo "==> Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "==> Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing systemd service..."
sudo cp telegram-bot.service /etc/systemd/system/telegram-bot.service
sudo sed -i "s|__USER__|$USER|g" /etc/systemd/system/telegram-bot.service
sudo sed -i "s|__HOME__|$HOME|g" /etc/systemd/system/telegram-bot.service

sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot

echo ""
echo "Done! Check status with: sudo systemctl status telegram-bot"

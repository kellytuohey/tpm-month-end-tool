#!/bin/bash
cd "$(dirname "$0")"

# Create virtual environment if needed
if [ ! -d "venv" ]; then
  echo "Setting up virtual environment (one-time)..."
  python3 -m venv venv
fi

source venv/bin/activate

# Install / update dependencies
pip install -r requirements.txt -q

# Run the app
python app.py

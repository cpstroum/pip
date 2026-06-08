#!/usr/bin/env bash
# Run once on the Unihiker M10 to install dependencies.
# The Unihiker ships with Python 3 and pip pre-installed.

set -e

echo "Installing Python dependencies…"
pip install -r requirements.txt

echo ""
echo "Make sure this env var is set before running pip.py:"
echo "  export OPENAI_API_KEY=your_key_here"
echo ""
echo "Then run:  python pip.py"

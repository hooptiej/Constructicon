#!/bin/bash
cd "$(dirname "$0")"
echo "Building ImageRepo Uploader..."
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on this Mac."
    echo "Install it from python.org (or 'brew install python3'), then run this again."
    read -n 1 -s -r -p "Press any key to close this window..."
    echo
    exit
fi

python3 -m venv venv
if [ ! -f venv/bin/activate ]; then
    echo "ERROR: failed to create a virtual environment in ./venv — see the output above for why."
    read -n 1 -s -r -p "Press any key to close this window..."
    echo
    exit
fi
source venv/bin/activate

pip install --upgrade pip --quiet
if ! pip install -r requirements.txt; then
    echo
    echo "ERROR: dependency install failed — see the pip output above for which package and why."
    read -n 1 -s -r -p "Press any key to close this window..."
    echo
    exit
fi

if ! python setup.py py2app; then
    echo
    echo "ERROR: py2app build failed — see the output above."
    echo "Common cause: Xcode command line tools missing — run 'xcode-select --install' then try again."
    read -n 1 -s -r -p "Press any key to close this window..."
    echo
    exit
fi

echo
echo "Done! Built: $(pwd)/dist/ImageRepo Uploader.app"
echo "Drag it to /Applications, open it, then paste in your imagerepo API token when prompted."
open dist/ 2>/dev/null

read -n 1 -s -r -p "Press any key to close this window..."
echo

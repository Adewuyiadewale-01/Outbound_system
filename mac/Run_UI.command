#!/bin/zsh
set -e

# Navigate to the app directory relative to this script
cd "$(dirname "$0")/../frontend"

echo "============================================="
echo "       Outreach Control Center Launcher       "
echo "============================================="
echo

# Install dependencies if they do not exist
if [ ! -d "node_modules" ]; then
  echo "Node modules not found. Installing Electron locally..."
  npm install
  echo "Dependencies installed successfully!"
  echo
fi

echo "Starting Electron Application..."
echo "You can close this terminal window; the app will keep running."
echo

# Run the app in the background and exit terminal script
npm start &!
exit 0

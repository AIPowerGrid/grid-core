#!/bin/bash

echo "🔄 Stopping all horde processes..."
pkill -f "server.py"

echo "⏳ Waiting for processes to terminate..."
sleep 3

echo "🚀 Starting horde processes on ports 7001-7008..."
for port in {7001..7008}; do
    echo "Starting horde on port $port..."
    sudo -u aipg nohup /usr/bin/python /home/aipg/aipg/server.py -vv --horde stable -p $port > /dev/null 2>&1 &
done

echo "✅ All horde processes restarted!"
echo "📊 Waiting 10 seconds for services to initialize..."
sleep 10

echo "🔍 Checking if services are responding..."
curl -s https://test.aipowergrid.io/ | head -5

echo "🎉 Restart complete!"

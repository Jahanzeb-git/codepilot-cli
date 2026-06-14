FROM ubuntu:24.04
RUN apt-get update && apt-get install -y python3 python3-pip build-essential bash && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
RUN pip3 install --break-system-packages --no-cache-dir codepilot-ai==0.9.3
COPY . .
CMD ["python3", "agent.py"]

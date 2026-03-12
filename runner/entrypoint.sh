#!/bin/bash

# Configuration
USER_HOME="/home/ubuntu"
SSH_DIR="$USER_HOME/.ssh"
KUBE_DIR="$USER_HOME/.kube"

# 1. Setup SSH directory and authorized_keys from secret mount
echo "Setting up SSH keys for ubuntu user..."
mkdir -p "$SSH_DIR"
if [ -f "/mnt/ssh/authorized_keys" ]; then
    cp /mnt/ssh/authorized_keys "$SSH_DIR/authorized_keys"
    chown -R ubuntu:ubuntu "$SSH_DIR"
    chmod 700 "$SSH_DIR"
    chmod 600 "$SSH_DIR/authorized_keys"
fi

# 2. Setup kubeconfig from secret mount
echo "Setting up kubeconfig..."
mkdir -p "$KUBE_DIR"
if [ -f "/mnt/kubeconfig/config" ]; then
    cp /mnt/kubeconfig/config "$KUBE_DIR/config"
    chown -R ubuntu:ubuntu "$KUBE_DIR"
    chmod 700 "$KUBE_DIR"
    chmod 600 "$KUBE_DIR/config"
fi

# 3. Generate SSH host keys if they don't exist
ssh-keygen -A

# 4. Start SSH daemon in foreground
echo "Starting SSH server..."
exec /usr/sbin/sshd -D

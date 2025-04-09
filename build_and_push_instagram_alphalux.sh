#!/bin/bash
# Script to build, tag, and push the Instagram connector to a private ECR repository.
#
# Prerequisites:
# - AWS CLI is configured and you are authenticated to your private ECR.
# - airbyte-ci is installed and available in your PATH.
# - Docker is installed and running.
#
# Usage: ./build_and_push_instagram.sh <version>
# Example: ./build_and_push_instagram.sh 1.0.0

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <version>"
  exit 1
fi

VERSION="$1"

#Login to ECR

aws ecr get-login-password --region eu-central-1 | docker login --username AWS --password-stdin 365865884601.dkr.ecr.eu-central-1.amazonaws.com

# Define the source image name that airbyte-ci builds.
SOURCE_IMAGE="airbyte/source-instagram:dev"

# Define the target image tag in your private ECR repository.
TARGET_IMAGE="365865884601.dkr.ecr.eu-central-1.amazonaws.com/airbyte-source-instagram-alphalux:alphalux-${VERSION}"

echo "Building Instagram connector..."
airbyte-ci connectors --name=source-instagram build

echo "Tagging image: ${SOURCE_IMAGE} -> ${TARGET_IMAGE}"
docker tag ${SOURCE_IMAGE} ${TARGET_IMAGE}

echo "Pushing image to private ECR..."
docker push ${TARGET_IMAGE}

echo "Build and push complete: ${TARGET_IMAGE}"


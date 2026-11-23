#!/usr/bin/env bash
# Experiment 1: successful release with a visible text change.
source "$(dirname "$0")/_common.sh"
start_branch exp/1-success
sed -i 's#cloud infrastructure.</p>#cloud infrastructure. This page was released automatically by a CI/CD pipeline.</p>#' site/index.html
grep -q "released automatically by a CI/CD pipeline" site/index.html
git commit -qam "Experiment 1: visible text change on the home page"
finish exp/1-success "Experiment 1: successful release" \
  "all four stages succeed; https://csce412isaacgeng.me/ shows the new sentence and the new short SHA in the footer; history.json marks the release verified."

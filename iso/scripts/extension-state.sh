#!/usr/bin/env bash

extension_state_from_info() {
    awk '/^[[:space:]]*State:/ { state = $NF } END { print state }'
}

extension_state_is_active() {
    [[ "$1" == "ACTIVE" || "$1" == "ENABLED" ]]
}

#!/bin/sh
set -eu

config_dir=/etc/nginx/fast-admin
cert_dir=/etc/letsencrypt/live/service.xxcdjl.xyz
mode=${NGINX_MODE:-auto}

certificate_ready() {
    [ -s "$cert_dir/fullchain.pem" ] &&
        [ -s "$cert_dir/privkey.pem" ] &&
        [ -s "$cert_dir/chain.pem" ]
}

case "$mode" in
    auto)
        if certificate_ready; then
            mode=https
        else
            mode=http
            echo "fast_admin: certificate files unavailable; starting HTTP on port 80"
        fi
        ;;
    http) ;;
    https)
        if ! certificate_ready; then
            echo "fast_admin: NGINX_MODE=https requires fullchain.pem, privkey.pem and chain.pem in $cert_dir" >&2
            exit 1
        fi
        ;;
    *)
        echo "fast_admin: invalid NGINX_MODE=$mode (expected auto, http or https)" >&2
        exit 1
        ;;
esac

cp "$config_dir/nginx.$mode.conf" /etc/nginx/nginx.conf
echo "fast_admin: selected Nginx $mode configuration"

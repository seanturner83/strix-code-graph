# Optional corporate-proxy CA certs

If you build this image behind a TLS-intercepting proxy (corporate egress,
Zscaler/Netskope-style), drop your proxy's root CA here as one or more `*.pem`
files. The Dockerfile's CA-injection step picks them up and trusts them for
in-container `curl`/`npm`/`pip`/`apt`/`git`, so the toolchain fetches succeed.

Absent (the default / clean-CI case) the step is skipped — the build reaches
public registries directly. Do **not** commit your organisation's CA here;
this directory is `.gitignore`d for `*.pem`/`*.crt`.

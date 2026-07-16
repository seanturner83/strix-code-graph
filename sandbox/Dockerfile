# syntax=docker/dockerfile:1
#
# strix-code-graph sandbox image (full multi-language).
#
# Derives from the base Strix sandbox and adds the SCIP toolchain + this addon,
# so a scan with STRIX_IMAGE pointed here can build a code-graph index
# in-container (via the register_session_setup hook). The toolchain is heavy
# (~2GB), which is why this is a SEPARATE opt-in image, not a base change.
#
# Legs (STRIX_CODE_GRAPH_LANGS selects which run per scan):
#   typescript  scip-typescript (npm)
#   python      scip-python (npm; Pyright under the hood)
#   go          scip-go (go install; base already ships go 1.26.x)
#   java        scip-java (Coursier) + JDK + maven/gradle
#   terraform   terraform-ls (bridged to SCIP by the addon)
#   kubernetes  helm + kustomize (rendered by the addon)
#   (rust: rust-analyzer is lazy-installed at runtime — not baked, ~500MB)
#
# Build args: UPSTREAM_TAG (base tag), TARGETARCH (amd64|arm64, set by buildx).
ARG UPSTREAM_TAG=1.0.0
FROM ghcr.io/usestrix/strix-sandbox:${UPSTREAM_TAG}

USER root
ARG TARGETARCH

# --- corporate TLS-interception CA (build-time only; no-op when absent) -------
# When the build runs behind a proxy (e.g. Netskope) drop its CA in
# sandbox/certs/ so in-container curl/npm/pip/apt trust it. Absent in CI = skip.
COPY sandbox/certs/ /usr/local/share/ca-certificates/extra/
RUN set -eux; \
    if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
      for f in /usr/local/share/ca-certificates/extra/*.pem; do \
        cp "$f" "/usr/local/share/ca-certificates/extra/$(basename "$f" .pem).crt"; \
      done; \
      update-ca-certificates; \
      printf '[global]\ncert = /etc/ssl/certs/ca-certificates.crt\n' > /etc/pip.conf; \
      su -s /bin/bash pentester -c 'npm config set cafile /etc/ssl/certs/ca-certificates.crt'; \
      git config --system http.sslCAInfo /etc/ssl/certs/ca-certificates.crt; \
    else echo "no extra CA certs — skipping"; fi

# --- scip CLI (SCIP -> SQLite) : single Go binary, SHA256-pinned (CWE-494) ----
ARG SCIP_VERSION=0.8.0
ARG SCIP_SHA256_AMD64=7e1d138d5ea043e95933d59939e89dc50038e9e3a3a8dee16db346a9c343ea5a
ARG SCIP_SHA256_ARM64=d97158be02123a5c4780b0d1bda0dbe2bf4a5670f853e90ea171e5c00ffa1b62
RUN set -eux; \
    arch="${TARGETARCH:-amd64}"; \
    case "$arch" in \
      amd64) sha="${SCIP_SHA256_AMD64}" ;; \
      arm64) sha="${SCIP_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH=$arch" >&2; exit 1 ;; \
    esac; \
    curl -fLsS "https://github.com/sourcegraph/scip/releases/download/v${SCIP_VERSION}/scip-linux-${arch}.tar.gz" -o /tmp/scip.tar.gz; \
    echo "${sha}  /tmp/scip.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/scip.tar.gz -C /usr/local/bin scip; \
    rm /tmp/scip.tar.gz; scip --version

# --- TS + Python legs : scip-typescript, scip-python (npm, prebuilt JS) -------
# --ignore-scripts: prebuilt bundles need no lifecycle scripts; skipping them
# removes a build-time arbitrary-code-execution surface.
RUN su -s /bin/bash pentester -c \
    'set -eux; \
     npm install -g --ignore-scripts --no-audit --no-fund \
        @sourcegraph/scip-typescript@0.4.0 @sourcegraph/scip-python@0.6.6; \
     /home/pentester/.npm-global/bin/scip-typescript --version; \
     /home/pentester/.npm-global/bin/scip-python --version'

# --- Go leg : scip-go via `go install` (arch-agnostic; base ships go 1.26.x) --
# GOTOOLCHAIN=local at runtime uses the baked go, not the target's go.mod pin
# (the pinned download is blocked by GOSUMDB=off). The base's 1.26.x satisfies
# our repos; we don't rebake go here (upstream-generic).
RUN su -s /bin/bash pentester -c \
    'set -eux; GOTOOLCHAIN=local go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7; \
     /home/pentester/go/bin/scip-go --version'

# --- Java leg : JDK + build tools + scip-java (Coursier) ----------------------
# scip-java drives the project's own build tool (mvn/gradle) to resolve the
# classpath, so it needs a JDK (javac) + build tools + scip-java itself.
#
# ENABLE_JAVA gates the leg. Default on (CI reaches Maven Central directly). Set
# to 0 for builds behind a TLS-interception proxy: Coursier resolves from Maven
# Central through its OWN JVM/truststore, which corporate-CA injection into the
# system + JVM cacerts does not reliably cover — a proxy-environment limitation,
# not an addon one. With it off the other legs still build; scip-java is simply
# absent and the java indexer leg degrades to "unavailable" like any missing
# toolchain. update-ca-certificates already refreshes the Debian java cacerts
# (/etc/ssl/certs/java) via the ca-certificates-java hook when a CA is present.
ARG ENABLE_JAVA=1
RUN set -eux; \
    if [ "$ENABLE_JAVA" = "1" ]; then \
      apt-get update; \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
          default-jdk maven gradle; \
      update-ca-certificates || true; \
      rm -rf /var/lib/apt/lists/*; \
      arch="${TARGETARCH:-amd64}"; \
      case "$arch" in \
        amd64) cs_url="https://github.com/coursier/coursier/releases/latest/download/cs-x86_64-pc-linux.gz" ;; \
        arm64) cs_url="https://github.com/VirtusLab/coursier-m1/releases/latest/download/cs-aarch64-pc-linux.gz" ;; \
        *) echo "unsupported TARGETARCH=$arch" >&2; exit 1 ;; \
      esac; \
      curl -fLsS "$cs_url" | gzip -d > /usr/local/bin/cs; \
      chmod +x /usr/local/bin/cs; \
      su -s /bin/bash pentester -c \
        'set -eux; export COURSIER_CACHE=/home/pentester/.cache/coursier; \
         /usr/local/bin/cs install --install-dir /home/pentester/.local/bin scip-java; \
         /home/pentester/.local/bin/scip-java version'; \
    else \
      echo "ENABLE_JAVA=0 — skipping scip-java (java leg will report unavailable)"; \
    fi

# --- Terraform leg : terraform-ls (HCL LSP, bridged to SCIP by the addon) -----
ARG TERRAFORM_LS_VERSION=0.36.5
RUN set -eux; \
    arch="${TARGETARCH:-amd64}"; \
    curl -fLsS "https://releases.hashicorp.com/terraform-ls/${TERRAFORM_LS_VERSION}/terraform-ls_${TERRAFORM_LS_VERSION}_linux_${arch}.zip" -o /tmp/tfls.zip; \
    unzip -o /tmp/tfls.zip -d /usr/local/bin terraform-ls; \
    rm /tmp/tfls.zip; terraform-ls --version

# --- Kubernetes leg : helm + kustomize (rendered by the addon) ----------------
ARG HELM_VERSION=3.16.4
ARG KUSTOMIZE_VERSION=5.5.0
RUN set -eux; \
    arch="${TARGETARCH:-amd64}"; \
    curl -fLsS "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${arch}.tar.gz" -o /tmp/helm.tar.gz; \
    tar -xzf /tmp/helm.tar.gz -C /tmp; \
    install -m 0755 "/tmp/linux-${arch}/helm" /usr/local/bin/helm; \
    rm -rf /tmp/helm.tar.gz "/tmp/linux-${arch}"; helm version --short; \
    curl -fLsS "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${KUSTOMIZE_VERSION}/kustomize_v${KUSTOMIZE_VERSION}_linux_${arch}.tar.gz" -o /tmp/kz.tar.gz; \
    tar -xzf /tmp/kz.tar.gz -C /usr/local/bin kustomize; \
    rm /tmp/kz.tar.gz; kustomize version

# --- the addon package : into the base venv (in-sandbox code needs only proto) -
# In-sandbox indexer/merge need protobuf + stdlib, NOT strix-agent (that's the
# runner-side tool-registration dep). --no-deps + explicit protobuf.
COPY --chown=pentester:pentester . /opt/strix-code-graph/src
RUN /app/.venv/bin/pip install --no-cache-dir "protobuf>=5.0" \
    && /app/.venv/bin/pip install --no-cache-dir --no-deps /opt/strix-code-graph/src \
    && /app/.venv/bin/python -c "import strix_code_graph.indexer, strix_code_graph.merge; print('strix-code-graph installed')"

USER pentester
WORKDIR /app

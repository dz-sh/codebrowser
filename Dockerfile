FROM ubuntu:24.04 AS builder

ARG DEBIAN_FRONTEND=noninteractive
ARG CODEBROWSER_REF=ebe91a86e4d06a23f5f7709d081f83e2cc14ef26

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        cmake \
        g++ \
        git \
        libclang-18-dev \
        llvm-18-dev \
        ninja-build \
    && rm -rf /var/lib/apt/lists/*

RUN git init /src/codebrowser \
    && git -C /src/codebrowser remote add origin https://github.com/KDAB/codebrowser.git \
    && git -C /src/codebrowser fetch --depth 1 origin "${CODEBROWSER_REF}" \
    && git -C /src/codebrowser checkout --detach FETCH_HEAD

RUN cmake \
        -S /src/codebrowser \
        -B /src/codebrowser/build \
        -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/codebrowser \
        -DLLVM_DIR=/usr/lib/llvm-18/lib/cmake/llvm \
        -DClang_DIR=/usr/lib/llvm-18/lib/cmake/clang \
    && cmake --build /src/codebrowser/build \
    && cmake --install /src/codebrowser/build \
    && install -D -m 0644 /src/codebrowser/LICENSE /opt/codebrowser/share/licenses/KDAB-codebrowser/LICENSE \
    && install -D -m 0644 /src/codebrowser/README.md /opt/codebrowser/share/licenses/KDAB-codebrowser/README.md

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        git \
        libclang-cpp18 \
        libllvm18 \
        passwd \
        python3 \
        python3-yaml \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --user-group --shell /usr/sbin/nologin codebrowser

COPY --from=builder /opt/codebrowser /opt/codebrowser
COPY src/codebrowser_control.py /usr/local/bin/codebrowser-control
COPY LICENSE /opt/codebrowser/share/licenses/codebrowser-deployment/LICENSE

ENV CODEBROWSER_GENERATOR=/opt/codebrowser/bin/codebrowser_generator \
    CODEBROWSER_INDEXGENERATOR=/opt/codebrowser/bin/codebrowser_indexgenerator \
    CODEBROWSER_DATA_DIR=/opt/codebrowser/share/woboq/data \
    CONFIG_ROOT=/config/repositories \
    OUTPUT_ROOT=/output

USER codebrowser
ENTRYPOINT ["codebrowser-control"]
CMD ["reconcile"]

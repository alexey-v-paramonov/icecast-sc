#!/bin/bash
# static_compile.sh — Build a fully static icecast binary
#
# Supported build hosts:
#   CentOS 7   (requires gcc-11 from devtoolset/SCL)
#   Ubuntu 20.04 (requires gcc-11, e.g. from ubuntu-toolchain-r/test PPA)
#   Ubuntu 22.04
#   Ubuntu 24.04
#
# Usage:
#   ./static_compile.sh [--jobs N] [--prefix /path]
#
# The script builds all dependencies from source into a private prefix so the
# resulting binary carries no runtime shared-library requirements beyond the
# glibc NSS stubs that are unavoidable on Linux (getaddrinfo, getpwnam, …).
#
# Key design decisions:
#   • -march=x86-64  forces the baseline ISA so no AVX/AVX2 SIMD routines from
#     libmvec are pulled in — avoids "Floating point exception" on older CPUs.
#   • Only the features that icecast actually uses are enabled in each dep.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configurable versions — update here when upstreams release new tarballs
# ---------------------------------------------------------------------------
VER_ZLIB=1.3.1
VER_XZ=5.6.3
VER_ZSTD=1.5.6
VER_XCRYPT=4.4.36
VER_OGG=1.3.5
VER_VORBIS=1.3.7
VER_SPEEX=1.2.1
VER_THEORA=1.1.1
VER_LIBXML2=2.14.5
VER_LIBXSLT=1.1.43
VER_OPENSSL=3.5.3
VER_CURL=8.14.1
VER_RHASH=1.4.6
VER_IGLOO=0.9.4

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
JOBS=$(nproc)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPS_PREFIX="${SCRIPT_DIR}/static-deps"
OUTPUT_BINARY="${SCRIPT_DIR}/icecast-static"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --jobs)   JOBS="$2"; shift 2 ;;
        --prefix) DEPS_PREFIX="$2"; shift 2 ;;
        --output) OUTPUT_BINARY="$2"; shift 2 ;;
        --help)
            echo "Usage: $0 [--jobs N] [--prefix PATH] [--output PATH]"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

BUILD_DIR="${DEPS_PREFIX}/build"
mkdir -p "${BUILD_DIR}"

# ---------------------------------------------------------------------------
# Detect OS / package manager and install build dependencies
# ---------------------------------------------------------------------------
detect_os() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        echo "${ID}${VERSION_ID}"
    elif [[ -f /etc/centos-release ]]; then
        echo "centos7"
    else
        echo "unknown"
    fi
}
OS_ID=$(detect_os)

install_build_deps() {
    case "${OS_ID}" in
        centos7*)
            # Assumes gcc-11 available via devtoolset-11 or similar
            yum install -y \
                wget tar make autoconf automake libtool pkgconfig \
                zlib-devel xz-devel libzstd-devel \
                python3 perl gettext
            ;;
        ubuntu20*)
            apt-get update -qq
            apt-get install -y \
                wget tar make autoconf automake libtool pkg-config \
                zlib1g-dev liblzma-dev libzstd-dev \
                python3 perl nasm gettext
            ;;
        ubuntu22*|ubuntu24*)
            apt-get update -qq
            apt-get install -y \
                wget tar make autoconf automake libtool pkg-config \
                zlib1g-dev liblzma-dev libzstd-dev \
                python3 perl nasm gettext
            ;;
        *)
            echo "WARNING: unknown OS '${OS_ID}', skipping automatic dep install."
            ;;
    esac
}

echo "==> Detected OS: ${OS_ID}"
echo "==> Installing build-time system dependencies..."
install_build_deps

# ---------------------------------------------------------------------------
# Detect GCC version and select compiler
# ---------------------------------------------------------------------------
find_gcc() {
    # Prefer an explicit versioned gcc (11–15) over the default
    for v in 15 14 13 12 11; do
        if command -v "gcc-${v}" &>/dev/null; then
            echo "gcc-${v}"
            return
        fi
    done
    # Fall back to whatever gcc is on PATH, check it is ≥ 11
    if command -v gcc &>/dev/null; then
        local ver
        ver=$(gcc -dumpversion | cut -d. -f1)
        if [[ "${ver}" -ge 11 ]]; then
            echo "gcc"
            return
        fi
    fi
    echo ""
}

find_gxx() {
    local gcc_cmd="$1"
    echo "${gcc_cmd/gcc/g++}"
}

CC_CMD=$(find_gcc)
if [[ -z "${CC_CMD}" ]]; then
    echo "ERROR: No GCC ≥ 11 found. Install gcc-11 or newer." >&2
    exit 1
fi
CXX_CMD=$(find_gxx "${CC_CMD}")
GCC_VERSION=$(${CC_CMD} -dumpversion | cut -d. -f1)
echo "==> Using compiler: ${CC_CMD} (GCC ${GCC_VERSION})"

# ---------------------------------------------------------------------------
# Common build flags
# ---------------------------------------------------------------------------
# -march=x86-64  — baseline ISA; no AVX/AVX2 so no libmvec IFUNC calls
# -O2            — optimise without auto-vectorisation to exotic ISAs
COMMON_CFLAGS="-O2 -march=x86-64 -fPIC"
export CC="${CC_CMD}"
export CXX="${CXX_CMD}"
# Prepend our prefix to the search path so our built libs take priority
# over any system versions, while still allowing pkg-config to resolve
# transitive system dependencies (e.g. zlib) that live in the system paths.
# Include lib64/pkgconfig for systems (e.g. CentOS/RHEL) where OpenSSL
# installs into lib64 rather than lib.
export PKG_CONFIG_PATH="${DEPS_PREFIX}/lib/pkgconfig:${DEPS_PREFIX}/lib64/pkgconfig${PKG_CONFIG_PATH:+:${PKG_CONFIG_PATH}}"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
fetch() {
    local url="$1" dest="$2"
    if [[ ! -f "${BUILD_DIR}/${dest}" ]]; then
        echo "  Downloading ${dest}..."
        wget -q -O "${BUILD_DIR}/${dest}" "${url}"
    else
        echo "  Already downloaded: ${dest}"
    fi
}

extract() {
    local archive="$1" dir="$2"
    if [[ ! -d "${BUILD_DIR}/${dir}" ]]; then
        echo "  Extracting ${archive}..."
        tar -xf "${BUILD_DIR}/${archive}" -C "${BUILD_DIR}"
    else
        echo "  Already extracted: ${dir}"
    fi
}

# ---------------------------------------------------------------------------
# On CentOS 7 the system automake is 1.13.4 but libigloo requires >= 1.14.
# Build and install a recent automake into the deps prefix.
# icecast-kh configure.ac requires autoconf >= 2.71; Ubuntu 20.04 and
# CentOS 7 both ship 2.69, so bootstrap autoconf from source too when needed.
# ---------------------------------------------------------------------------
VER_AUTOMAKE=1.16.5
VER_AUTOCONF=2.72

bootstrap_automake() {
    echo
    echo "==> Bootstrapping automake ${VER_AUTOMAKE} (system version too old)..."
    fetch "https://ftpmirror.gnu.org/automake/automake-${VER_AUTOMAKE}.tar.gz" \
          "automake-${VER_AUTOMAKE}.tar.gz"
    extract "automake-${VER_AUTOMAKE}.tar.gz" "automake-${VER_AUTOMAKE}"
    pushd "${BUILD_DIR}/automake-${VER_AUTOMAKE}" >/dev/null
        ./configure --prefix="${DEPS_PREFIX}"
        make -j"${JOBS}"
        make install
    popd >/dev/null
    echo "==> automake now: $(automake --version | head -1)"
}

bootstrap_autoconf() {
    echo
    echo "==> Bootstrapping autoconf ${VER_AUTOCONF} (system version too old)..."
    fetch "https://ftpmirror.gnu.org/autoconf/autoconf-${VER_AUTOCONF}.tar.gz" \
          "autoconf-${VER_AUTOCONF}.tar.gz"
    extract "autoconf-${VER_AUTOCONF}.tar.gz" "autoconf-${VER_AUTOCONF}"
    pushd "${BUILD_DIR}/autoconf-${VER_AUTOCONF}" >/dev/null
        ./configure --prefix="${DEPS_PREFIX}"
        make -j"${JOBS}"
        make install
    popd >/dev/null
    echo "==> autoconf now: $(autoconf --version | head -1)"
}

if [[ "${OS_ID}" == centos7* ]]; then
    bootstrap_automake
fi

# Bootstrap autoconf if the system version is < 2.71.
# icecast-kh configure.ac has AC_PREREQ([2.71]).
# Ubuntu 20.04 ships 2.69; CentOS 7 ships 2.69.
_ac_ver=$(autoconf --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1 || echo "0.0")
_ac_major="${_ac_ver%%.*}"
_ac_minor="${_ac_ver##*.}"
if [[ "${_ac_major}" -lt 2 ]] || { [[ "${_ac_major}" -eq 2 ]] && [[ "${_ac_minor}" -lt 71 ]]; }; then
    bootstrap_autoconf
fi
unset _ac_ver _ac_major _ac_minor

# Save the original PATH before we prepend our private prefix.
# Libigloo's autoreconf must use a fully consistent set of system autotools;
# mixing our bootstrapped autoconf 2.72 binary with the system autom4te/aclocal
# causes autoconf's own standard macros to become invisible (AC_MSG_ERROR etc.).
ORIGINAL_PATH="${PATH}"

# Ensure our private bin dir is always first on PATH (new autoconf/automake/etc.)
export PATH="${DEPS_PREFIX}/bin:${PATH}"

# On CentOS 7 the bootstrapped aclocal must also search the system macro dir
# so that standard autoconf macros (AC_MSG_ERROR etc.) are found.
if [[ "${OS_ID}" == centos7* ]]; then
    export ACLOCAL_PATH="/usr/share/aclocal${ACLOCAL_PATH:+:${ACLOCAL_PATH}}"
fi

# ---------------------------------------------------------------------------
# 0a. zlib
# ---------------------------------------------------------------------------
echo
echo "==> Building zlib ${VER_ZLIB}..."
fetch "https://github.com/madler/zlib/releases/download/v${VER_ZLIB}/zlib-${VER_ZLIB}.tar.gz" \
      "zlib-${VER_ZLIB}.tar.gz"
extract "zlib-${VER_ZLIB}.tar.gz" "zlib-${VER_ZLIB}"
pushd "${BUILD_DIR}/zlib-${VER_ZLIB}" >/dev/null
    # zlib configure uses env-var CFLAGS, not a positional argument
    CFLAGS="${COMMON_CFLAGS}" ./configure \
        --prefix="${DEPS_PREFIX}" \
        --static
    make -j"${JOBS}"
    make install
popd >/dev/null

# ---------------------------------------------------------------------------
# 0b. xz / liblzma
# ---------------------------------------------------------------------------
echo
echo "==> Building xz ${VER_XZ}..."
fetch "https://github.com/tukaani-project/xz/releases/download/v${VER_XZ}/xz-${VER_XZ}.tar.gz" \
      "xz-${VER_XZ}.tar.gz"
extract "xz-${VER_XZ}.tar.gz" "xz-${VER_XZ}"
pushd "${BUILD_DIR}/xz-${VER_XZ}" >/dev/null
    ./configure \
        --prefix="${DEPS_PREFIX}" \
        --enable-static \
        --disable-shared \
        --disable-xz \
        --disable-xzdec \
        --disable-lzmadec \
        --disable-lzmainfo \
        --disable-scripts \
        --disable-doc \
        CFLAGS="${COMMON_CFLAGS}"
    make -j"${JOBS}"
    make install
popd >/dev/null

# ---------------------------------------------------------------------------
# 0c. zstd
# ---------------------------------------------------------------------------
echo
echo "==> Building zstd ${VER_ZSTD}..."
fetch "https://github.com/facebook/zstd/releases/download/v${VER_ZSTD}/zstd-${VER_ZSTD}.tar.gz" \
      "zstd-${VER_ZSTD}.tar.gz"
extract "zstd-${VER_ZSTD}.tar.gz" "zstd-${VER_ZSTD}"
pushd "${BUILD_DIR}/zstd-${VER_ZSTD}" >/dev/null
    # Build only the static library using zstd's lib Makefile
    make -j"${JOBS}" -C lib libzstd.a \
        CC="${CC_CMD}" CFLAGS="${COMMON_CFLAGS}"
    cp lib/libzstd.a "${DEPS_PREFIX}/lib/"
    mkdir -p "${DEPS_PREFIX}/include"
    cp lib/zstd.h lib/zstd_errors.h lib/zdict.h "${DEPS_PREFIX}/include/"
    # Generate pkg-config file
    mkdir -p "${DEPS_PREFIX}/lib/pkgconfig"
    cat > "${DEPS_PREFIX}/lib/pkgconfig/libzstd.pc" <<EOF
prefix=${DEPS_PREFIX}
exec_prefix=\${prefix}
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: libzstd
Description: fast lossless compression algorithm library
Version: ${VER_ZSTD}
Libs: -L\${libdir} -lzstd
Cflags: -I\${includedir}
EOF
popd >/dev/null

# ---------------------------------------------------------------------------
# 0d. libxcrypt  (self-contained crypt_r — replaces NSS-backed system libcrypt
#               on CentOS 7 which cannot be statically linked)
# ---------------------------------------------------------------------------
echo
echo "==> Building libxcrypt ${VER_XCRYPT}..."
fetch "https://github.com/besser82/libxcrypt/releases/download/v${VER_XCRYPT}/libxcrypt-${VER_XCRYPT}.tar.xz" \
      "libxcrypt-${VER_XCRYPT}.tar.xz"
extract "libxcrypt-${VER_XCRYPT}.tar.xz" "libxcrypt-${VER_XCRYPT}"
pushd "${BUILD_DIR}/libxcrypt-${VER_XCRYPT}" >/dev/null
    ./configure \
        --prefix="${DEPS_PREFIX}" \
        --enable-static \
        --disable-shared \
        --disable-werror \
        CFLAGS="${COMMON_CFLAGS}"
    make -j"${JOBS}"
    make install
popd >/dev/null

# ---------------------------------------------------------------------------
# 1. libogg
# ---------------------------------------------------------------------------
echo
echo "==> Building libogg ${VER_OGG}..."
fetch "https://downloads.xiph.org/releases/ogg/libogg-${VER_OGG}.tar.xz" \
      "libogg-${VER_OGG}.tar.xz"
extract "libogg-${VER_OGG}.tar.xz" "libogg-${VER_OGG}"
pushd "${BUILD_DIR}/libogg-${VER_OGG}" >/dev/null
    ./configure \
        --prefix="${DEPS_PREFIX}" \
        --enable-static \
        --disable-shared \
        CFLAGS="${COMMON_CFLAGS}"
    make -j"${JOBS}"
    make install
popd >/dev/null

# ---------------------------------------------------------------------------
# 2. libvorbis
# ---------------------------------------------------------------------------
echo
echo "==> Building libvorbis ${VER_VORBIS}..."
fetch "https://downloads.xiph.org/releases/vorbis/libvorbis-${VER_VORBIS}.tar.xz" \
      "libvorbis-${VER_VORBIS}.tar.xz"
extract "libvorbis-${VER_VORBIS}.tar.xz" "libvorbis-${VER_VORBIS}"
pushd "${BUILD_DIR}/libvorbis-${VER_VORBIS}" >/dev/null
    ./configure \
        --prefix="${DEPS_PREFIX}" \
        --enable-static \
        --disable-shared \
        --with-ogg="${DEPS_PREFIX}" \
        CFLAGS="${COMMON_CFLAGS}"
    make -j"${JOBS}"
    make install
popd >/dev/null

# ---------------------------------------------------------------------------
# 3. speex
# ---------------------------------------------------------------------------
echo
echo "==> Building speex ${VER_SPEEX}..."
fetch "https://downloads.xiph.org/releases/speex/speex-${VER_SPEEX}.tar.gz" \
      "speex-${VER_SPEEX}.tar.gz"
extract "speex-${VER_SPEEX}.tar.gz" "speex-${VER_SPEEX}"
pushd "${BUILD_DIR}/speex-${VER_SPEEX}" >/dev/null
    ./configure \
        --prefix="${DEPS_PREFIX}" \
        --enable-static \
        --disable-shared \
        CFLAGS="${COMMON_CFLAGS}"
    make -j"${JOBS}"
    make install
popd >/dev/null

# ---------------------------------------------------------------------------
# 4. libtheora
# ---------------------------------------------------------------------------
echo
echo "==> Building libtheora ${VER_THEORA}..."
fetch "https://downloads.xiph.org/releases/theora/libtheora-${VER_THEORA}.tar.xz" \
      "libtheora-${VER_THEORA}.tar.xz"
extract "libtheora-${VER_THEORA}.tar.xz" "libtheora-${VER_THEORA}"
pushd "${BUILD_DIR}/libtheora-${VER_THEORA}" >/dev/null
    ./configure \
        --prefix="${DEPS_PREFIX}" \
        --enable-static \
        --disable-shared \
        --with-ogg="${DEPS_PREFIX}" \
        --with-vorbis="${DEPS_PREFIX}" \
        --disable-examples \
        --disable-doc \
        CFLAGS="${COMMON_CFLAGS}"
    make -j"${JOBS}"
    make install
popd >/dev/null

# ---------------------------------------------------------------------------
# 5. libxml2
# ---------------------------------------------------------------------------
echo
echo "==> Building libxml2 ${VER_LIBXML2}..."
fetch "https://download.gnome.org/sources/libxml2/$(echo ${VER_LIBXML2} | cut -d. -f1-2)/libxml2-${VER_LIBXML2}.tar.xz" \
      "libxml2-${VER_LIBXML2}.tar.xz"
extract "libxml2-${VER_LIBXML2}.tar.xz" "libxml2-${VER_LIBXML2}"
pushd "${BUILD_DIR}/libxml2-${VER_LIBXML2}" >/dev/null
    ./configure \
        --prefix="${DEPS_PREFIX}" \
        --enable-static \
        --disable-shared \
        --without-python \
        --without-lzma \
        --with-zlib \
        CFLAGS="${COMMON_CFLAGS}"
    make -j"${JOBS}"
    make install
popd >/dev/null

# ---------------------------------------------------------------------------
# 6. libxslt
# ---------------------------------------------------------------------------
echo
echo "==> Building libxslt ${VER_LIBXSLT}..."
fetch "https://download.gnome.org/sources/libxslt/$(echo ${VER_LIBXSLT} | cut -d. -f1-2)/libxslt-${VER_LIBXSLT}.tar.xz" \
      "libxslt-${VER_LIBXSLT}.tar.xz"
extract "libxslt-${VER_LIBXSLT}.tar.xz" "libxslt-${VER_LIBXSLT}"
pushd "${BUILD_DIR}/libxslt-${VER_LIBXSLT}" >/dev/null
    ./configure \
        --prefix="${DEPS_PREFIX}" \
        --enable-static \
        --disable-shared \
        --without-python \
        --with-libxml-prefix="${DEPS_PREFIX}" \
        CFLAGS="${COMMON_CFLAGS}"
    make -j"${JOBS}"
    make install
popd >/dev/null

# ---------------------------------------------------------------------------
# 7. OpenSSL
# ---------------------------------------------------------------------------
echo
echo "==> Building OpenSSL ${VER_OPENSSL}..."
fetch "https://www.openssl.org/source/openssl-${VER_OPENSSL}.tar.gz" \
      "openssl-${VER_OPENSSL}.tar.gz"
extract "openssl-${VER_OPENSSL}.tar.gz" "openssl-${VER_OPENSSL}"
pushd "${BUILD_DIR}/openssl-${VER_OPENSSL}" >/dev/null
    ./Configure \
        linux-x86_64 \
        no-shared \
        no-zstd \
        no-gost \
        no-tests \
        --prefix="${DEPS_PREFIX}" \
        --libdir="${DEPS_PREFIX}/lib" \
        --openssldir="${DEPS_PREFIX}/etc/ssl" \
        CC="${CC_CMD}" \
        CFLAGS="${COMMON_CFLAGS}"
    make -j"${JOBS}"
    make install_sw   # install_sw skips docs/manpages
popd >/dev/null

# ---------------------------------------------------------------------------
# 8. libcurl (minimal: HTTP + HTTPS only, no GSSAPI/Kerberos)
# ---------------------------------------------------------------------------
echo
echo "==> Building libcurl ${VER_CURL}..."
fetch "https://curl.se/download/curl-${VER_CURL}.tar.xz" \
      "curl-${VER_CURL}.tar.xz"
extract "curl-${VER_CURL}.tar.xz" "curl-${VER_CURL}"
pushd "${BUILD_DIR}/curl-${VER_CURL}" >/dev/null
    ./configure \
        --prefix="${DEPS_PREFIX}" \
        --enable-static \
        --disable-shared \
        --with-openssl="${DEPS_PREFIX}" \
        --without-gssapi \
        --without-brotli \
        --without-zstd \
        --without-libidn2 \
        --without-libpsl \
        --without-libssh2 \
        --without-nghttp2 \
        --without-nghttp3 \
        --without-librtmp \
        --disable-ldap \
        --disable-ldaps \
        --disable-manual \
        CFLAGS="${COMMON_CFLAGS}"
    make -j"${JOBS}"
    make install
popd >/dev/null

# ---------------------------------------------------------------------------
# 9. RHash
# ---------------------------------------------------------------------------
echo
echo "==> Building RHash ${VER_RHASH}..."
fetch "https://github.com/rhash/RHash/archive/refs/tags/v${VER_RHASH}.tar.gz" \
      "RHash-${VER_RHASH}.tar.gz"
extract "RHash-${VER_RHASH}.tar.gz" "RHash-${VER_RHASH}"
pushd "${BUILD_DIR}/RHash-${VER_RHASH}" >/dev/null
    # RHash uses its own ./configure wrapper (not autoconf)
    ./configure \
        --prefix="${DEPS_PREFIX}" \
        --disable-openssl \
        --cc="${CC_CMD}" \
        --extra-cflags="${COMMON_CFLAGS}"
    make -j"${JOBS}" lib-static
    make install-lib-static install-lib-headers
popd >/dev/null

# fix up the pkg-config file that RHash doesn't always install
if [[ ! -f "${DEPS_PREFIX}/lib/pkgconfig/librhash.pc" ]]; then
    mkdir -p "${DEPS_PREFIX}/lib/pkgconfig"
    cat > "${DEPS_PREFIX}/lib/pkgconfig/librhash.pc" <<EOF
prefix=${DEPS_PREFIX}
exec_prefix=\${prefix}
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: librhash
Description: RHash library
Version: ${VER_RHASH}
Libs: -L\${libdir} -lrhash
Cflags: -I\${includedir}
EOF
fi

# ---------------------------------------------------------------------------
# 10. libigloo
# ---------------------------------------------------------------------------
echo
echo "==> Building libigloo ${VER_IGLOO}..."
fetch "https://gitlab.xiph.org/xiph/icecast-libigloo/-/archive/v${VER_IGLOO}/icecast-libigloo-v${VER_IGLOO}.tar.gz" \
      "icecast-libigloo-v${VER_IGLOO}.tar.gz"
# Always re-extract so our m4/acx_pthread.m4 patch is applied to a clean tree.
# (The tarball is cached locally so this is fast.)
rm -rf "${BUILD_DIR}/icecast-libigloo-v${VER_IGLOO}"
extract "icecast-libigloo-v${VER_IGLOO}.tar.gz" "icecast-libigloo-v${VER_IGLOO}"
pushd "${BUILD_DIR}/icecast-libigloo-v${VER_IGLOO}" >/dev/null
    # The bundled acx_pthread.m4 uses the obsolete AC_TRY_LINK macro.  When
    # autoreconf regenerates configure with autoconf 2.72 the compat shim for
    # AC_TRY_LINK silently returns false for every link test (observed on
    # CentOS 7 + devtoolset-11), making all pthread probes fail even with
    # -lpthread.  Replace the macro with a trivial stub before autoreconf so
    # the generated configure just uses -lpthread unconditionally.
    # Patch acx_pthread.m4 on all platforms: the bundled version uses the
    # obsolete AC_TRY_LINK/AC_LANG_C macros whose compat shims in modern
    # autoconf can interact badly with m4 diversions and cause cascading
    # "possibly undefined macro" errors for unrelated macros.
    cat > m4/acx_pthread.m4 <<'ENDOFM4'
AC_DEFUN([ACX_PTHREAD],[
  AC_SUBST(PTHREAD_LIBS)
  AC_SUBST(PTHREAD_CFLAGS)
  test -n "$PTHREAD_LIBS"  || PTHREAD_LIBS="-lpthread"
  test -n "$PTHREAD_CFLAGS" || PTHREAD_CFLAGS=""
  AC_MSG_CHECKING([for POSIX threads])
  AC_MSG_RESULT([yes])
])
ENDOFM4
    if [[ "${OS_ID}" == centos7* ]]; then
        # CentOS 7: system automake 1.13.4 is too old; use our bootstrapped tools.
        autoreconf -fi
    else
        # Ubuntu: run autoreconf with the original system PATH so that autoreconf,
        # aclocal, autoconf, autoheader, and automake are all consistent system
        # versions.  Mixing our bootstrapped autoconf 2.72 binary (which may be
        # present in deps/bin from a previous run) with the system autom4te causes
        # autoconf's own standard macros to become invisible.
        env PATH="${ORIGINAL_PATH}" autoreconf -fi
    fi
    ./configure \
        --prefix="${DEPS_PREFIX}" \
        CFLAGS="${COMMON_CFLAGS}" \
        LIBS="-lm"
    make -j"${JOBS}"
    make install
popd >/dev/null

# ---------------------------------------------------------------------------
# 11. Icecast itself
# ---------------------------------------------------------------------------
echo
echo "==> Configuring Icecast..."
pushd "${SCRIPT_DIR}" >/dev/null

# Always regenerate configure so it is compatible with the local automake.
# Use the original system PATH so that autoreconf, aclocal, autoconf, and
# automake are all consistent system versions.  If a bootstrapped autoconf
# was installed into DEPS_PREFIX/bin it will have the old prefix hard-coded
# in its Perl @INC and will fail to find autoconf's own modules.
env PATH="${ORIGINAL_PATH}" autoreconf -fi

# Re-run configure unconditionally so it picks up all our fresh deps.
# icecast-kh uses XIPH_PATH_OGG/VORBIS/SPEEX/THEORA macros that resolve
# libraries via --with-<lib>=PREFIX (not pkg-config), so we must pass each
# prefix explicitly.  xml2/xslt are found via *-config scripts on PATH.
./configure \
    --prefix="/usr" \
    --with-ogg="${DEPS_PREFIX}" \
    --with-vorbis="${DEPS_PREFIX}" \
    --with-speex="${DEPS_PREFIX}" \
    --with-theora="${DEPS_PREFIX}" \
    --with-curl="${DEPS_PREFIX}" \
    --with-openssl="${DEPS_PREFIX}" \
    CC="${CC_CMD}" \
    CFLAGS="${COMMON_CFLAGS}"

# configure may not detect xsltSaveResultToString when libxslt is not on the
# default search path.  Force the flag so the #ifndef guard in src/xslt.c
# does NOT emit a second definition that collides with the one in libxslt.a.
sed -i 's|.*#undef HAVE_XSLTSAVERESULTTOSTRING.*|#define HAVE_XSLTSAVERESULTTOSTRING 1|' config.h

echo
echo "==> Building static Icecast binary..."

# All transitive deps are built from source into DEPS_PREFIX.
# -ldl   : libcrypto.a(dso_dlfcn.o) calls dlopen/dlsym for engine loading
# -lcrypt: resolves to our built libxcrypt (self-contained, no NSS dep)
EXTRA_LIBS="-lm -lz -llzma -lzstd -lcrypt -ldl"

make all \
    LDFLAGS="-all-static" \
    LIBS="$(pkg-config --libs igloo libxml-2.0 libxslt vorbis ogg speex theora libcurl openssl librhash libzstd 2>/dev/null) -pthread ${EXTRA_LIBS}"

popd >/dev/null

# ---------------------------------------------------------------------------
# Copy result to output path
# ---------------------------------------------------------------------------
cp -f "${SCRIPT_DIR}/src/icecast" "${OUTPUT_BINARY}"
strip "${OUTPUT_BINARY}"

echo
echo "==> Done. Static binary: ${OUTPUT_BINARY}"
echo "    Size: $(du -sh "${OUTPUT_BINARY}" | cut -f1)"
ldd "${OUTPUT_BINARY}" 2>&1 || true

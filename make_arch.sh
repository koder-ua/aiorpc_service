#!/usr/bin/env bash
set -o errexit
set -o pipefail
set -o nounset

UNPACK=$(dirname "${0}")
readonly UNPACK="${UNPACK}/unpack.sh"
readonly DOCKER="${1}"
readonly OUTPUT="${2:-/tmp/aiorpc_service.sh}"
readonly ROOT=/opt/root
readonly ARCH=package.tar.xz
readonly SRC_ROOT="${3:-/home/koder/workspace}"

LOCAL_PACKAGES="${SRC_ROOT}/aiorpc "
LOCAL_PACKAGES+="${SRC_ROOT}/aiorpc_service "
LOCAL_PACKAGES+="${SRC_ROOT}/cephlib "
LOCAL_PACKAGES+="${SRC_ROOT}/koder_utils "
LOCAL_PACKAGES+="${SRC_ROOT}/ceph_report "
LOCAL_PACKAGES+="${SRC_ROOT}/xmlbuilder3"
readonly LOCAL_PACKAGES

DEBS="https://launchpadlibrarian.net/416520700/python3.7-minimal_3.7.3-1+xenial1_amd64.deb"
DEBS+=" https://launchpadlibrarian.net/416520694/python3.7-distutils_3.7.3-1+xenial1_all.deb"
DEBS+=" https://launchpadlibrarian.net/416520689/libpython3.7-stdlib_3.7.3-1+xenial1_amd64.deb"
DEBS+=" https://launchpadlibrarian.net/416520688/libpython3.7-minimal_3.7.3-1+xenial1_amd64.deb"
readonly DEBS

readonly PIPOPTS="--no-warn-script-location"
readonly PYTHON="${ROOT}/usr/bin/python3.7"

function dexec {
    docker exec --interactive "${DOCKER}" "${@}"
}

function clear {
    dexec rm --recursive --force "${ROOT}"
}

function install_tools {
    dexec apt update
    dexec apt install --assume-yes unzip curl libexpat1 xz-utils
}

function download_debs {
    for url in ${DEBS} ; do
        fname=$(basename "${url}")
        dexec curl -o "/tmp/${fname}" "${url}"
    done
}

function install_debs {
    for url in ${DEBS} ; do
        fname=$(basename "${url}")
        dexec dpkg-deb --extract "/tmp/${fname}" "${ROOT}"
    done
}

function install_pip {
    dexec curl https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    dexec "${PYTHON}" /tmp/get-pip.py --prefix /opt/root/usr
    dexec mkdir --parents /opt/root/usr/local/lib/python3.7/dist-packages
    echo /opt/root/usr/lib/python3.7/site-packages | dexec tee /opt/root/usr/local/lib/python3.7/dist-packages/pip.pth
}

function install_local_packages {
    for package in ${LOCAL_PACKAGES} ; do
        local name=$(basename "${package}")
        dexec rm --recursive --force "/tmp/${name}"
        docker cp "${package}" "${DOCKER}:/tmp"
        dexec "${PYTHON}" -m pip install ${PIPOPTS} "/tmp/${name}" --no-deps
    done
}

function install_local_packages_deps {
    for package in ${LOCAL_PACKAGES} ; do
        local name=$(basename ${package})
        dexec "${PYTHON}" -m pip install ${PIPOPTS} -r "/tmp/${name}/requirements.txt"
    done
    dexec cp --recursive /tmp/aiorpc_service/aiorpc_service_files /opt/root/usr/local/lib/python3.7/dist-packages
}

function remove_pip {
    dexec rm --recursive --force /opt/root/usr/lib/python3.7/site-packages
}

function remove_extra_files {
    dexec rm --recursive --force "${ROOT}/usr/share"
    for directory in $(dexec find "${ROOT}" -iname __pycache__) ; do
        dexec rm --recursive --force "${directory}"
    done

#    echo "rm ${ROOT}/**/__pycache__" | dexec bash
#    dexec find "${ROOT}" -name "*.dist-info" -delete
#    dexec find "${ROOT}" -name "*.c" -delete
#    dexec find "${ROOT}" -name "*.pyx" -delete
#    dexec find "${ROOT}" -name "*.pxi" -delete
#    dexec find "${ROOT}" -name "*.pyi" -delete
}

function pack {
    dexec tar --create --xz --file "/tmp/${ARCH}" --directory "${ROOT}" .
}



clear
#install_tools
#download_debs
install_debs
install_pip
install_local_packages
install_local_packages_deps
remove_pip
remove_extra_files
pack
docker cp "${DOCKER}:/tmp/${ARCH}" /tmp
cat "${UNPACK}" "/tmp/${ARCH}" > "${OUTPUT}"
ls -l --human-readable "${OUTPUT}"
md5sum "${OUTPUT}"

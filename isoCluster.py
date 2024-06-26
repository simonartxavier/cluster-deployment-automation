import sys
import os
from pathlib import Path
import shutil
import time
import urllib.parse
from logger import logger
from clustersConfig import ClustersConfig
from clustersConfig import NodeConfig
from dhcpConfig import dhcp_config_from_file, DHCPD_CONFIG_PATH, DHCPD_CONFIG_BACKUP_PATH, CDA_TAG, get_subnet_range
import host
import common


"""
ExtraConfigIPU is used to provision and IPUs specified via Redfish through the IMC.
This works by making some assumptions about the current state of the IPU:
- The IMC is on MeV 1.2 / Mev 1.3
- BMD_CONF has been set to allow for iso Boot
- ISCSI attempt has been added to allow for booting into the installed media
- The specified ISO contains full installation kickstart / kargs required for automated boot
- The specified ISO handles installing dependencies like dhclient and microshift
- The specified ISO architecture is aarch64
- There is an additional connection between the provisioning host and the acc on an isolated subnet to serve dhcp / provide acc with www
"""


def render_dhcpd_conf(mac: str, ip: str, name: str) -> None:
    logger.debug("Rendering dhcpd conf")
    file_path = DHCPD_CONFIG_PATH

    # If a config already exists, check if it was generated by CDA.
    file = Path(file_path)
    if file.exists():
        logger.debug("Existing dhcpd configuration detected")
        with file.open('r') as f:
            line = f.readline()
        # If not created by CDA, save as a backup to maintain idempotency
        if CDA_TAG not in line:
            logger.info(f"Backing up existing dhcpd conf to {DHCPD_CONFIG_BACKUP_PATH}")
            shutil.move(file_path, DHCPD_CONFIG_BACKUP_PATH)
    file.touch()

    dhcp_config = dhcp_config_from_file(DHCPD_CONFIG_PATH)

    dhcp_config.add_host(hostname=name, hardware_ethernet=mac, fixed_address=ip)

    dhcp_config.write_to_file()


def configure_dhcpd(node: NodeConfig) -> None:
    logger.info("Configuring dhcpd entry")

    render_dhcpd_conf(node.mac, str(node.ip), node.name)
    lh = host.LocalHost()
    ret = lh.run("systemctl restart dhcpd")
    if ret.returncode != 0:
        logger.error(f"Failed to restart dhcpd with err: {ret.err}")
        sys.exit(-1)


def configure_iso_network_port(api_port: str, node_ip: str) -> None:
    start, _ = get_subnet_range(node_ip, "255.255.255.0")
    lh = host.LocalHost()
    logger.info(f"Flushing cluster port {api_port} and setting ip to {start}")
    lh.run_or_die(f"ip addr flush dev {api_port}")
    lh.run_or_die(f"ip addr add {start}/24 dev {api_port}")


def enable_acc_connectivity(node: NodeConfig) -> None:
    logger.info(f"Establishing connectivity to {node.name}")
    ipu_imc = host.RemoteHost(node.bmc)
    ipu_imc.ssh_connect(node.bmc_user, node.bmc_password)
    # ipu_imc.run_or_die("/usr/bin/scripts/cfg_acc_apf_x2.py")
    # """
    # We need to ensure the ACC physical port connectivity is enabled during reboot to ensure dhcp gets an ip.
    # Trigger an acc reboot and try to run python /usr/bin/scripts/cfg_acc_apf_x2.py. This will fail until the
    # ACC_LAN_APF_VPORTs are ready. Once this succeeds, we can try to connect to the ACC
    # """
    logger.info("Rebooting IMC to trigger ACC reboot")
    ipu_imc.run("systemctl reboot")
    time.sleep(30)
    # ipu_imc.ssh_connect(node.bmc_user, node.bmc_password)
    # logger.info(f"Attempting to enable ACC connectivity from IMC {node.bmc} on reboot")
    # retries = 30
    # for _ in range(retries):
    #     ret = ipu_imc.run("/usr/bin/scripts/cfg_acc_apf_x2.py")
    #     if ret.returncode == 0:
    #         logger.info("Enabled ACC physical port connectivity")
    #         break
    #     logger.debug(f"ACC SPF script failed with returncode {ret.returncode}")
    #     logger.debug(f"out: {ret.out}\n err: {ret.err}")
    #     time.sleep(15)
    # else:
    #     logger.error_and_exit("Failed to enable ACC connectivity")

    ipu_acc = host.RemoteHost(str(node.ip))
    ipu_acc.ping()
    ipu_acc.ssh_connect("root", "redhat")
    logger.info(f"{node.name} connectivity established")


def is_http_url(url: str) -> bool:
    try:
        result = urllib.parse.urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False


def _redfish_boot_ipu(cc: ClustersConfig, node: NodeConfig, iso: str) -> None:
    def helper(node: NodeConfig) -> str:
        logger.info(f"Booting {node.bmc} with {iso_address}")
        bmc = host.BMC.from_bmc(node.bmc)
        bmc.boot_iso_redfish(iso_path=iso_address, retries=5, retry_delay=15)

        imc = host.Host(node.bmc)
        imc.ssh_connect(node.bmc_user, node.bmc_password)
        # TODO: Remove once https://issues.redhat.com/browse/RHEL-32696 is solved
        time.sleep(25 * 60)
        return f"Finished booting imc {node.bmc}"

    # Ensure dhcpd is stopped before booting the IMC to avoid unintentionally setting the ACC hostname during the installation
    # https://issues.redhat.com/browse/RHEL-32696
    lh = host.LocalHost()
    lh.run("systemctl stop dhcpd")

    # If an http address is provided, we will boot from here.
    # Otherwise we will assume a local file has been provided and host it.
    if is_http_url(iso):
        logger.debug(f"Booting IPU from iso served at {iso}")
        iso_address = iso

        logger.info(helper(node))
    else:
        logger.debug(f"Booting IPU from local iso {iso}")
        if not os.path.exists(iso):
            logger.error(f"ISO file {iso} does not exist, exiting")
            sys.exit(-1)
        serve_path = os.path.dirname(iso)
        iso_name = os.path.basename(iso)
        lh = host.LocalHost()
        cc.prepare_external_port()
        lh_ip = common.port_to_ip(lh, cc.external_port)

        with common.HttpServerManager(serve_path, 8000) as http_server:
            iso_address = f"http://{lh_ip}:{str(http_server.port)}/{iso_name}"
            logger.info(helper(node))


def IPUIsoBoot(cc: ClustersConfig, node: NodeConfig, iso: str) -> None:
    _redfish_boot_ipu(cc, node, iso)
    assert node.ip is not None
    configure_iso_network_port(cc.network_api_port, node.ip)
    configure_dhcpd(node)
    enable_acc_connectivity(node)


def main() -> None:
    pass


if __name__ == "__main__":
    main()

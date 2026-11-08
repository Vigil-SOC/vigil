"""Lint fails a rule on a value tied to one site, and no other: the values public
rule corpora (SigmaHQ, Splunk ESCU, Elastic detection-rules) use that name no
site pass, the site values beside them still fail, and the corpus rules lint
cannot judge are listed with why."""

import pytest

from core.detections.lint import lint_query, lint_sigma


def _sigma(selection: str, condition: str = "sel") -> str:
    return (
        "title: t\nlogsource: {product: windows}\ndetection:\n"
        f"  sel: {selection}\n  condition: {condition}\n"
    )


def _values(report):
    return [f["value"] for f in report.get("findings", [])]


# (corpus rule it comes from, language, query or Sigma selection)
PASS = [
    # Partial values on a user field are no one account.
    (
        "proc_creation_win_susp_system_user_anomaly",
        "sigma",
        "{User|contains: [AUTHORI, AUTORI]}",
    ),
    (
        "win_security_ad_user_enumeration",
        "sigma",
        "{SubjectUserName|startswith: MSOL_}",
    ),
    (
        "win_security_susp_computer_name",
        "sigma",
        "{SamAccountName|startswith: SAMTHEADMIN-}",
    ),
    (
        "win_security_user_added_to_local_administrators",
        "sigma",
        "{TargetUserName|startswith: Administr}",
    ),
    (
        "win_security_susp_local_anon_logon_created",
        "sigma",
        "{SamAccountName|contains|all: [ANONYMOUS, LOGON]}",
    ),
    (
        "net_connection_win_susp_remote_powershell_session",
        "sigma",
        "{User|contains: NETZWERKDIENST}",
    ),
    # A fieldref names another field.
    (
        "win_security_explicit_credential_local_logon",
        "sigma",
        "{SubjectUserName|fieldref: TargetUserName}",
    ),
    (
        "proc_creation_win_susp_cross_user_process_spawn",
        "sigma",
        "{User|fieldref: ParentUser}",
    ),
    # Built-in, system and product accounts and groups.
    (
        "win_system_service_install_pua_proceshacker",
        "sigma",
        "{AccountName: LocalSystem}",
    ),
    (
        "windows_privileged_group_modification",
        "splunk",
        'TargetUserName IN ("DnsAdmins", "Replicators")',
    ),
    (
        "windows_special_privileged_logon_on_multiple_hosts",
        "splunk",
        'Caller_User_Name IN ("DWM-1", "DWM-2", "DWM-3")',
    ),
    (
        "windows_identify_powershell_web_access_iis_pool",
        "splunk",
        'SubjectUserName="pswa_pool"',
    ),
    (
        "cisco_sd_wan_multiple_source_ip_vmanage_admin_ssh_authentication",
        "splunk",
        'user="vmanage-admin"',
    ),
    (
        "o365_exfiltration_via_file_access",
        "splunk",
        'Operation IN ("fileaccessed") UserId!=app@sharepoint',
    ),
    (
        "windows_domain_admin_impersonation_indicator",
        "splunk",
        '| fillnull value=NotDA username | search username = "NotDA"',
    ),
    (
        "persistence_web_server_sus_child_spawned",
        "lucene",
        "user.name:(lighttpd or tomcat8 or tomcat9)",
    ),
    (
        "persistence_ssh_via_backdoored_system_user",
        "lucene",
        "user.name:(lp or man or news or uucp or messagebus or systemd-network or avahi or dnsmasq)",
    ),
    (
        "defense_evasion_interactive_shell_from_system_user",
        "lucene",
        "user.name:(_apt or Debian-exim or polkitd or rtkit or pipewire or tss)",
    ),
    (
        "discovery_multiple_discovery_api_calls_via_cli",
        "lucene",
        "user.name:AWSServiceRoleForConfig",
    ),
    (
        "credential_access_azure_aks_coredns_configmap_modified",
        "esql",
        'FROM logs-* | WHERE azure.platformlogs.properties.log.user.username NOT IN ("aksService", "hcpService", "masterclient", "nodeclient")',
    ),
    (
        "privilege_escalation_kubernetes_api_request_impersonating_privileged_identity",
        "eql",
        'any where kubernetes.audit.impersonatedUser.username in ("cluster-admin", "kubernetes-admin")',
    ),
    (
        "privilege_escalation_gcp_gke_suspicious_assignment_of_controller_service_account",
        "lucene",
        "gcp.audit.request.spec.serviceAccountName:(attachdetach-controller or cloud-provider or generic-garbage-collector or persistent-volume-binder)",
    ),
    (
        "initial_access_teams_rogue_helpdesk_chat",
        "lucene",
        "user.name:(support or helpdesk or itadmin or ithelp or it)",
    ),
    ("azure_legacy_authentication_protocols", "sigma", "{Username: 'UPN'}"),
    # Platform addresses and ranges every install shares.
    (
        "credential_access_suspicious_instance_metadata_service_api_request",
        "eql",
        'network where destination.ip == "169.254.169.254"',
    ),
    (
        "credential_access_azure_wireserver_unusual_process_connection",
        "eql",
        'network where destination.ip == "168.63.129.16"',
    ),
    (
        "credential_access_web_server_cloud_imds_ssrf_request",
        "lucene",
        "url.query:(*169.254.170.2* or *100.100.100.200* or *metadata.google.internal*)",
    ),
    (
        "credential_access_secret_file_access_followed_by_imds_request",
        "eql",
        'network where cidrmatch(destination.ip, "169.254.169.254/32", "fd00:ec2::254/128", "fd00:ec2::23/128")',
    ),
    (
        "persistence_ssh_netcon",
        "eql",
        'network where not cidrmatch(destination.ip, "10.0.0.0/8", "172.31.0.0/16")',
    ),
    (
        "credential_access_entra_id_excessive_account_lockouts",
        "esql",
        'FROM logs-* | WHERE NOT CIDR_MATCH(source.ip, "fd00::/8")',
    ),
    (
        "defense_evasion_vpc_security_group_ingress_rule_added_for_remote_connections",
        "lucene",
        "aws.cloudtrail.request_parameters:(*0.0.0.0/0* or *::/0*)",
    ),
    # Documentation, benchmarking, multicast and protocol blocks name no site.
    (
        "command_and_control_script_interpreter_connection_to_non_standard_port",
        "eql",
        'network where not cidrmatch(destination.ip, "233.252.0.0/24", "198.19.0.0/16", "192.18.0.0/15")',
    ),
    (
        "command_and_control_frequent_egress_netcon_from_sus_executable",
        "eql",
        'network where not cidrmatch(destination.ip, "192.0.0.29/32")',
    ),
    # Provider ranges (Microsoft's) are no one site's.
    (
        "net_connection_win_rundll32_net_connections",
        "sigma",
        "{DestinationIp|cidr: [20.0.0.0/8, 51.103.0.0/16, 2.16.56.0/23]}",
    ),
    # Files and paths that end in an environment TLD.
    (
        "persistence_rc_script_creation",
        "eql",
        'file where file.path == "/etc/rc.local"',
    ),
    (
        "linux_file_creation_in_init_boot_directory",
        "splunk",
        'Filesystem.file_path IN ("*/etc/rc.local*")',
    ),
    ("lnx_shell_susp_commands", "sigma", "{keywords: [rc.local]}"),
    (
        "image_load_side_load_comctl32",
        "sigma",
        "{ImageLoaded|startswith: ['C:\\Windows\\System32\\logonUI.exe.local', 'C:\\Windows\\System32\\werFault.exe.local']}",
    ),
    ("fortinet_fortigate_new_local_user_created", "sigma", "{cfgpath: user.local}"),
    ("zeek_dns_susp_zbit_flag", "sigma", "{query|endswith: .local}"),
    (
        "persistence_suspicious_command_execution_from_vscode",
        "esql",
        'FROM logs-* | WHERE NOT TO_LOWER(process.command_line) RLIKE """.*(docker.internal|localhost).*"""',
    ),
    (
        "credential_access_anthropic_session_reuse_multi_countries",
        "esql",
        'FROM logs-* | WHERE `user.email` != "unknown@invalid.internal"',
    ),
    # Versions that look like addresses.
    (
        "proxy_ua_apt",
        "sigma",
        "{c-useragent: 'Mozilla/5.0 (Windows; U; Windows NT 5.1; en-US; rv:1.9.2.13) Gecko'}",
    ),
    ("proxy_ua_malware", "sigma", "{c-useragent: '1.9.2.3'}"),
    (
        "proc_creation_win_susp_service_tamper",
        "sigma",
        "{CommandLine|contains: FontCache3.0.0.0}",
    ),
]

PASS += [
    # Vendor built-in accounts (#4105).
    (
        "splunk_edit_user_privilege_escalation",
        "splunk",
        "NOT user IN (admin, splunk-system-user)",
    ),
    ("splunk_dos_via_printf_search_function", "splunk", 'user="splunk_system_user"'),
    (
        "kubernetes_azure_detect_sensitive_object_access",
        "splunk",
        'user.username="system.anonymous"',
    ),
    (
        "lateral_movement_unusual_process_sql_accounts",
        "eql",
        'process where user.name : ("SQLSERVERAGENT", "MSSQLSERVER", '
        '"MSSQLServerOLAPService", "MsDtsServer150", "SQLWriter", "winmgmt")',
    ),
    ("symantec_ws_admin_login", "sigma", "{TargetUserName: virtadmin}"),
]

# Values tied to one site: these still fail, next to the cases above.
FAIL = [
    ("splunk", 'index=win user="jdoe"', "jdoe"),
    ("splunk", r'index=win user="CORP\jdoe"', "jdoe"),
    ("lucene", "user.name:(lp or jdoe)", "jdoe"),
    ("sigma", "{TargetUserName: j.smith}", "j.smith"),
    ("splunk", '| fillnull value=NotDA username | search username="jdoe"', "jdoe"),
    ("eql", 'network where destination.ip == "10.20.30.40"', "10.20.30.40"),
    ("eql", 'network where destination.ip == "8.8.8.8"', "8.8.8.8"),
    ("eql", 'network where cidrmatch(source.ip, "10.20.30.0/24")', "10.20.30.0/24"),
    ("eql", 'network where cidrmatch(source.ip, "172.31.5.0/24")', "172.31.5.0/24"),
    (
        "eql",
        'network where cidrmatch(source.ip, "fd12:3456:789a::/48")',
        "fd12:3456:789a::/48",
    ),
    (
        "eql",
        'network where cidrmatch(source.ip, "198.51.100.0/28") or cidrmatch(source.ip, "52.10.20.0/28")',
        "52.10.20.0/28",
    ),
    ("sigma", "{DestinationIp|cidr: 52.10.20.16/28}", "52.10.20.16/28"),
    (
        "eql",
        'process where process.command_line : "*dc01.corp.local*"',
        "dc01.corp.local",
    ),
    (
        "eql",
        r'file where file.path : "\\\\fs01.corp.local\\share\\*"',
        "fs01.corp.local",
    ),
    ("splunk", 'index=ldap url="ldap://dc01.corp.local/"', "dc01.corp.local"),
    ("sigma", "{CommandLine|contains: .corp.local}", ".corp.local"),
    ("sigma", "{Image|endswith: '\\cmd.exe', SourceIp: 10.1.2.3}", "10.1.2.3"),
]

# Corpus rules lint still fails, and why it can't tell: each names a value no
# site owns, but it reads the same as one that does.
REMAINING = [
    (
        "zeek_rdp_public_listener",
        "sigma",
        "{id.orig_h|cidr: '2620:83:8000::/48'}",
        "a public /48: a site's own IPv6 block is that size",
    ),
    (
        "net_connection_win_office_outbound_non_local_ip",
        "sigma",
        "{DestinationIp|cidr: [204.79.197.0/24, 13.107.6.152/31]}",
        "Microsoft ranges narrower than /23: a site's public block is that size",
    ),
    (
        "win_security_diagtrack_eop_default_login_username",
        "sigma",
        "{TargetOutboundUserName: thisisnotvaliduser}",
        "an exploit's hard-coded user name reads as a person's",
    ),
    (
        "persistence_ssh_via_backdoored_system_user",
        "lucene",
        "user.name:(daemon or deamon)",
        "a typo of daemon in the rule",
    ),
    (
        "defense_evasion_interactive_shell_from_system_user",
        "lucene",
        "user.name:(games or gamer or libstoreagemgmt)",
        "typos of games and libstoragemgmt in the rule",
    ),
]


def _lint(language, text):
    if language == "sigma":
        return lint_sigma(rule_yaml=_sigma(text))
    return lint_query(text, language)


@pytest.mark.parametrize("rule,language,text", PASS, ids=[c[0] for c in PASS])
def test_a_value_that_names_no_site_passes(rule, language, text):
    report = _lint(language, text)
    assert report["passed"], _values(report)


@pytest.mark.parametrize("language,text,value", FAIL, ids=[c[2] for c in FAIL])
def test_a_value_tied_to_one_site_still_fails(language, text, value):
    report = _lint(language, text)
    assert not report["passed"]
    assert value in _values(report)


@pytest.mark.parametrize(
    "rule,language,text,why", REMAINING, ids=[c[0] for c in REMAINING]
)
def test_the_listed_corpus_rules_still_fail(rule, language, text, why):
    assert not _lint(language, text)["passed"], why

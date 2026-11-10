// Hosts the dev server answers to, read from VIGIL_ALLOWED_HOSTS: names separated
// by commas or spaces. A leading dot admits subdomains (".example.com"). "true"
// turns Vite's host check off. Unset or blank returns undefined so the caller
// leaves server.allowedHosts alone and Vite keeps its own default. When it is set
// the list is used as given, with no defaults added to it.
export function parseAllowedHosts(raw) {
    var value = raw === null || raw === void 0 ? void 0 : raw.trim();
    if (!value)
        return undefined;
    if (value.toLowerCase() === 'true')
        return true;
    var hosts = value.split(/[\s,]+/).filter(Boolean);
    return hosts.length ? hosts : undefined;
}

# Third-party notices and original-source license status

MultiModelCouncil is an independent application built with [Microsoft Agent Framework](https://github.com/microsoft/agent-framework). Microsoft provides the underlying agent/workflow primitives, provider integrations and original DevUI. No Microsoft endorsement or affiliation is claimed.

The framework is MIT licensed. Its copyright and permission notice is preserved in [licenses/Microsoft-Agent-Framework-MIT.txt](licenses/Microsoft-Agent-Framework-MIT.txt). This notice applies to applicable Microsoft material, including the narrow DevUI transport integration; it does not assign Microsoft's license to independently authored Council code.

Framework and provider SDK packages are installed dependencies. Their source trees, environments and built DevUI assets are not bundled here. Each dependency retains its own license and notices.

No redistribution license has been selected for the original Council source. Public repository visibility does not constitute an additional license grant from the author. A future license decision belongs to the author.

The transport safeguard verifies the installed DevUI bundle and inserts a small guard in the served bytes. It changes neither upstream SDK source nor installed package files, and adds no replacement interface.

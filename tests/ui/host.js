// Minimal MCP Apps host for tests: renders the panel in a sandboxed iframe and
// bridges it with the SDK's own AppBridge. Tool calls are forwarded to the real
// MCP server through window.hostCallTool (provided by the Node test runner).
import { AppBridge, PostMessageTransport } from "@modelcontextprotocol/ext-apps/app-bridge";

window.hostLog = { messages: [], modelContext: [], toolCalls: [] };

window.startHost = async (panelHtml, theme) => {
  const iframe = document.createElement("iframe");
  iframe.setAttribute("sandbox", "allow-scripts allow-forms");
  iframe.style.cssText = "width:640px;height:1200px;border:0;display:block";
  iframe.id = "panel";
  document.body.style.background = theme === "dark" ? "#1f1e1c" : "#ffffff";
  document.body.appendChild(iframe);

  const bridge = new AppBridge(
    null,
    { name: "test-host", version: "1.0.0" },
    { serverTools: {}, message: { text: {} }, updateModelContext: { text: {} } },
    { hostContext: { theme, platform: "desktop", displayMode: "inline" } },
  );
  bridge.oncalltool = async (params) => {
    window.hostLog.toolCalls.push(params.name);
    return await window.hostCallTool(JSON.stringify(params)).then(JSON.parse);
  };
  bridge.onmessage = async (params) => {
    window.hostLog.messages.push(params);
    return {};
  };
  bridge.onupdatemodelcontext = async (params) => {
    window.hostLog.modelContext.push(params);
    return {};
  };
  bridge.onsizechange = ({ height }) => {
    if (height) iframe.style.height = `${Math.ceil(height)}px`;
  };

  await bridge.connect(new PostMessageTransport(iframe.contentWindow, iframe.contentWindow));
  iframe.srcdoc = panelHtml;
};

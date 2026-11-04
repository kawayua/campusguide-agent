"""W8 冷白 + STSong 主题 CSS（Gradio 注入）。"""

THEME_CSS = r"""
:root{
  --ink:#1f2a30;
  --ink-2:#48565e;
  --muted:#6d7a82;
  --faint:#9ba7ae;
  --paper:#eef2f4;
  --surface:#ffffff;
  --surface-2:#f2f6f8;
  --surface-3:#e8eef0;
  --line:#e3e9ec;
  --line-strong:#cfd9de;
  --green:#2f6f62;
  --green-dark:#1f5349;
  --green-soft:#e9f3f0;
  --green-line:#cfe3dd;
  --amber:#9a6b22;
  --amber-soft:#fbf4e3;
  --amber-line:#ecd7ae;
  --blue:#356fa8;
  --blue-soft:#ebf2fa;
  --blue-line:#cddff1;
  --red:#a0473f;
  --red-soft:#fbf0ee;
  --red-line:#ead0ca;
  --gray-soft:#edf2f4;
  --gray-line:#d9e1e5;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --serif:"STSong","Source Han Serif SC","Noto Serif CJK SC","Noto Serif SC","Songti SC",serif;
  --mono:"SFMono-Regular","Cascadia Code",Consolas,monospace;
}
html,body{height:100%}
body{
  margin:0;
  background:
    radial-gradient(1000px 560px at 12% -10%,rgba(47,111,98,.075),transparent 60%),
    radial-gradient(850px 480px at 92% 0%,rgba(53,111,168,.05),transparent 58%),
    linear-gradient(180deg,#f1f5f6,#e7edef);
  color:var(--ink);
  font-family:var(--sans);
}
html,body{background:#eef2f4!important;color-scheme:light!important}
.gradio-container{
  max-width:none!important;
  width:100%!important;
  padding:0!important;
  background:transparent!important;
  font-family:var(--sans)!important;
}
.gradio-container > .main,.gradio-container > .wrap{max-width:none!important;width:100%!important}
.main.fillable{padding:0!important}
footer{display:none!important}
#w8-root{
  height:100vh!important;
  min-height:640px;
  padding:14px!important;
  gap:0!important;
  flex-wrap:nowrap!important;
  align-items:stretch!important;
  box-sizing:border-box;
}
#w8-history,#w8-chat,#w8-side{
  box-sizing:border-box;
  min-height:0;
}
#w8-history{
  flex:0 0 264px!important;
  max-width:264px!important;
  height:100%;
  display:flex!important;
  flex-direction:column!important;
  flex-wrap:nowrap!important;
  background:#f4f8f9;
  border:1px solid var(--line);
  border-right:0;
  border-radius:16px 0 0 16px;
  padding:14px 13px!important;
  gap:9px!important;
  overflow:hidden;
}
#w8-chat{
  flex:1 1 auto!important;
  min-width:0;
  height:100%;
  display:flex!important;
  flex-direction:column!important;
  flex-wrap:nowrap!important;
  gap:0!important;
  padding:0!important;
  background:#fbfcfd;
  border-top:1px solid var(--line);
  border-bottom:1px solid var(--line);
  overflow:hidden;
}
#w8-side{
  flex:0 0 430px!important;
  max-width:430px!important;
  height:100%;
  display:flex!important;
  flex-direction:column!important;
  flex-wrap:nowrap!important;
  gap:0!important;
  padding:0!important;
  background:#f6f9fa;
  border:1px solid var(--line);
  border-left:0;
  border-radius:0 16px 16px 0;
  overflow-y:auto;
}
#w8-side.panel-hidden{
  flex:0 0 0!important;
  width:0!important;
  max-width:0!important;
  min-width:0!important;
  padding:0!important;
  border:0!important;
  opacity:0;
  overflow:hidden!important;
  pointer-events:none;
}
#w8-history .block,#w8-chat .block,#w8-side .block{box-shadow:none!important}

/* ---------------- history rail ---------------- */
.w8-brand{display:flex;align-items:center;gap:9px;padding:2px 2px 3px}
.w8-brand-mark{
  width:32px;height:32px;border-radius:9px;flex:0 0 auto;
  display:grid;place-items:center;
  background:radial-gradient(120% 120% at 20% 0%,#2b7c5b,#155039 68%);
  color:#fff;font-family:var(--serif);font-size:12px;font-weight:700;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.18);
}
.w8-brand h1{font-family:var(--serif);font-size:18px;line-height:1.2;margin:0;font-weight:700}
.w8-brand p{font-size:9px;color:var(--faint);letter-spacing:.08em;margin:2px 0 0}
#w8-new-chat{
  width:100%!important;
  min-height:34px!important;
  border:1px solid var(--line-strong)!important;
  background:#fff!important;
  color:var(--ink-2)!important;
  border-radius:8px!important;
  font-size:11.5px!important;
  font-weight:600!important;
}
#w8-new-chat:hover{border-color:var(--green)!important;color:var(--green-dark)!important;background:var(--green-soft)!important}
#w8-history-search{background:transparent!important;border:0!important;border-radius:8px!important;padding:0!important}
#w8-history-search label{background:rgba(255,255,255,.58)!important;border:1px solid transparent!important;border-radius:8px!important;padding:3px 7px!important;min-height:30px!important}
#w8-history-search:focus-within label{background:#fff!important;border-color:var(--line-strong)!important}
#w8-history-search textarea,#w8-history-search input{
  min-height:24px!important;height:24px!important;font-size:10.5px!important;padding:2px 2px!important;overflow:hidden!important;
}
#w8-history-list{background:transparent!important;border:0!important;padding:0!important;flex:1!important;min-height:0;overflow-y:auto;display:flex!important;flex-direction:column!important}
#w8-history-list .wrap{display:flex!important;flex-direction:column!important;gap:2px!important}
#w8-history-list label{
  display:block!important;
  margin:0!important;
  padding:8px 9px!important;
  border:1px solid transparent!important;
  border-radius:8px!important;
  background:transparent!important;
  color:var(--ink)!important;
  font-family:var(--serif)!important;
  font-size:12.2px!important;
  font-weight:600!important;
  line-height:1.45!important;
  white-space:pre-line!important;
  cursor:pointer;
}
#w8-history-list label:hover{background:#eaf1f3!important}
#w8-history-list label:has(input:checked){
  background:#fff!important;border-color:var(--line)!important;
  box-shadow:0 1px 2px rgba(30,45,55,.05);
}
#w8-history-list input[type="radio"]{display:none!important}
#w8-history-list .selected{background:#fff!important;border-color:var(--line)!important}
.w8-history-footer{margin-top:auto;padding:4px 2px 0}
.w8-history-foot-row{display:flex;align-items:center;justify-content:space-between;gap:6px}
.w8-profile-badge{
  display:inline-flex;align-items:center;gap:5px;
  color:var(--green-dark);background:var(--green-soft);border:1px solid var(--green-line);
  border-radius:99px;padding:3px 8px;font-size:8.8px;font-weight:650;
}
.w8-profile-badge .w8-dot{width:5px;height:5px;border-radius:50%;background:var(--green)}
.w8-history-note{display:flex;flex-direction:column;gap:2px;font-size:8.4px;line-height:1.5;color:var(--faint);margin-top:7px}
.w8-history-note .w8-privacy{color:var(--red)}
#w8-reset{min-height:27px!important;font-size:9.5px!important;background:#fff!important;border:1px solid var(--line)!important;color:var(--muted)!important;border-radius:6px!important}

/* ---------------- chat header ---------------- */
#w8-chat-head{
  flex:0 0 auto;
  display:flex!important;
  flex-wrap:nowrap!important;
  align-items:center!important;
  gap:8px!important;
  padding:11px 20px!important;
  background:rgba(255,255,255,.92);
  border-bottom:1px solid var(--line);
}
#w8-conversation-head{flex:1 1 auto!important;min-width:0!important;max-width:100%!important}
#w8-conversation-head .html-container{width:100%!important}
.w8-conversation-head{display:flex;align-items:center;justify-content:space-between;gap:10px;min-width:0}
.w8-conversation-title{min-width:0}
.w8-conversation-title h2{
  margin:0;font-family:var(--serif);font-size:16px;font-weight:700;line-height:1.35;
  color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.w8-conversation-title p{margin:2px 0 0;font-size:10px;line-height:1.4;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.w8-conversation-badges{display:flex;align-items:center;gap:5px;flex-wrap:wrap;justify-content:flex-end}
.w8-profile-note{font-size:8px;color:var(--faint);border-left:1px solid var(--line);padding-left:5px}
#w8-panel-toggle{
  flex:0 0 auto!important;min-width:52px!important;min-height:29px!important;
  border:1px solid var(--line)!important;background:#fff!important;color:var(--ink-2)!important;
  border-radius:8px!important;font-size:10px!important;font-weight:600!important;
}

/* ---------------- chatbot ---------------- */
#w8-chatbot{
  flex:1 1 auto!important;
  min-height:0!important;
  height:auto!important;
  max-height:none!important;
  padding:10px 20px!important;
  border:0!important;
  background:transparent!important;
  overflow:hidden!important;
}
#w8-chatbot .bubble-wrap{
  height:100%!important;
  overflow-y:auto!important;
  padding-bottom:10px!important;
}
#w8-chatbot .message.user,#w8-chatbot .message.bot{gap:9px!important;margin-bottom:16px!important;width:100%!important;max-width:100%!important}
#w8-chatbot .message.user{justify-content:flex-end!important}
#w8-chatbot .message.user > .message .message-content{width:max-content!important;max-width:100%!important}
#w8-chatbot .message.user > .message{
  background:linear-gradient(135deg,var(--green),var(--green-dark))!important;
  border:0!important;
  color:#fff!important;
  border-radius:16px 16px 5px 16px!important;
  font-size:12.8px!important;line-height:1.65!important;
  width:max-content!important;max-width:82%!important;
  padding:10px 14px!important;
  box-shadow:0 6px 16px rgba(47,111,98,.18)!important;
}
#w8-chatbot .message.user > .message .md,
#w8-chatbot .message.user > .message p,
#w8-chatbot .message.user > .message li,
#w8-chatbot .message.user > .message a,
#w8-chatbot .message.user > .message code{color:#fff!important}
#w8-chatbot .message.bot > .message{
  background:#fff!important;
  border:1px solid var(--line)!important;
  color:var(--ink-2)!important;
  border-radius:13px!important;
  font-size:12.8px!important;line-height:1.65!important;
  width:100%!important;max-width:100%!important;
  padding:14px 16px!important;
  box-shadow:0 1px 2px rgba(27,38,31,.04);
}
#w8-chatbot .icon-button{display:none!important}
[data-testid="status-tracker"]{display:none!important}
#w8-chatbot .message .md{font-size:12.8px!important}
#w8-chatbot .message p{margin:5px 0!important}
#w8-chatbot .message ul,#w8-chatbot .message ol{margin:5px 0!important;padding-left:20px!important}
#w8-chatbot .message blockquote{
  margin:7px 0!important;padding:5px 9px!important;
  border-left:2px solid var(--green-line)!important;background:var(--surface-2)!important;
  color:var(--muted)!important;font-size:11.4px!important;
}
.w8-turn-badges{display:inline-flex;gap:4px;flex-wrap:wrap;margin-bottom:5px}
#w8-welcome{flex:1 1 auto;min-height:0;display:flex!important;align-items:center;justify-content:center;padding:18px 24px;overflow-y:auto}
.w8-welcome{width:100%;display:flex;justify-content:center}
.w8-welcome-card{
  width:min(680px,100%);padding:26px 28px 22px;border:1px solid var(--line);border-radius:16px;
  background:radial-gradient(600px 220px at 0% 0%,rgba(47,111,98,.07),transparent 65%),linear-gradient(135deg,#fff,#f7f9fa);
  box-shadow:0 1px 2px rgba(27,38,31,.03);
}
.w8-welcome-kicker{font-size:9px;letter-spacing:.16em;color:var(--green);font-weight:700}
.w8-welcome-card h2{font-family:var(--serif);font-size:25px;line-height:1.35;margin:6px 0 5px;color:var(--ink)}
.w8-welcome-card p{margin:0;font-size:11.6px;line-height:1.75;color:var(--muted);max-width:600px}
.w8-welcome-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:14px}
.w8-welcome-grid div{display:flex;gap:7px;align-items:flex-start;padding:8px 9px;border:1px solid var(--line);border-radius:8px;background:rgba(255,255,255,.72);font-size:10.5px;line-height:1.5;color:var(--ink-2)}
.w8-welcome-grid b{font-family:var(--mono);font-size:8.5px;color:var(--green);background:var(--green-soft);border:1px solid var(--green-line);border-radius:4px;padding:1px 4px;font-weight:600}
.w8-welcome-tags{display:flex;gap:5px;flex-wrap:wrap;margin-top:13px;padding-top:11px;border-top:1px dashed var(--line)}
.w8-welcome-tags span{font-size:8.8px;color:var(--muted);border:1px solid var(--line);border-radius:99px;padding:2px 7px;background:#fff}

/* ---------------- progress / clarify / composer ---------------- */
#w8-status{padding:6px 20px 0!important;flex:0 0 auto}
.w8-progress-stepper{
  display:inline-flex;align-items:center;width:max-content;max-width:100%;
  padding:6px 11px;border:1px solid var(--line);border-radius:999px;
  background:rgba(255,255,255,.94);box-shadow:0 2px 8px rgba(30,55,60,.05);
  color:var(--ink-2);
}
.w8-progress-dots{display:flex;align-items:center;gap:0;list-style:none;margin:0;padding:0;flex:0 1 auto;min-width:0}
.w8-progress-step{display:flex;align-items:center;gap:4px;flex:0 0 auto;color:var(--faint);font-size:9px}
.w8-progress-step:not(:last-child)::after{
  content:"";width:14px;flex:0 0 14px;margin:0 6px;border-radius:99px;background:var(--line);
}
.w8-progress-step.done::after{background:var(--green-line)}
.w8-progress-dot{
  width:7px;height:7px;border:1px solid var(--line-strong);border-radius:50%;
  background:#fff;box-shadow:none;flex:0 0 auto;
  transition:background .18s ease,border-color .18s ease,box-shadow .18s ease;
}
.w8-progress-step.done .w8-progress-dot{
  background:#fff;border-color:var(--green);box-shadow:0 0 0 2px rgba(47,111,98,.07);
}
.w8-progress-step.active .w8-progress-dot{
  background:var(--green);border-color:var(--green);
  box-shadow:0 0 0 3px rgba(47,111,98,.14);
  animation:w8-dot-pulse 1.4s ease-in-out infinite;
}
.w8-progress-step.done .w8-progress-label{color:var(--muted)}
.w8-progress-step.active .w8-progress-label{color:var(--green-dark);font-weight:700}
.w8-progress-label{white-space:nowrap;font-size:8.8px;letter-spacing:.02em}
@keyframes w8-dot-pulse{
  0%,100%{box-shadow:0 0 0 3px rgba(47,111,98,.14)}
  50%{box-shadow:0 0 0 5px rgba(47,111,98,.04)}
}
.w8-status-message{
  padding:8px 11px;border:1px solid var(--line);border-left:3px solid var(--amber);
  border-radius:9px;background:#fff;color:var(--ink-2);font-size:10px;line-height:1.5;
}
#w8-clarify-prompt{padding:0 20px!important;flex:0 0 auto}
.w8-clarify-prompt{
  display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;
  padding:7px 10px;border:1px solid var(--blue-line);background:var(--blue-soft);
  border-radius:9px;font-size:10.5px;color:var(--blue);
}
.w8-clarify-prompt strong{font-weight:650}
.w8-clarify-prompt span{color:var(--muted);font-size:9.5px}
#w8-clarify-row{padding:5px 20px 0!important;gap:5px!important;flex-wrap:wrap}
#w8-clarify-row button{
  min-height:27px!important;padding:3px 11px!important;
  border:1px solid var(--blue-line)!important;background:#fff!important;
  color:var(--blue)!important;border-radius:99px!important;font-size:10.5px!important;
}
#w8-skip{color:var(--muted)!important;border-color:var(--line)!important}
#w8-examples{padding:4px 20px 0!important;gap:5px!important;flex-wrap:wrap;justify-content:flex-start!important}
#w8-examples button{
  flex:0 0 auto!important;min-width:0!important;width:auto!important;
  min-height:23px!important;padding:2px 8px!important;
  border:1px solid var(--line)!important;background:#fff!important;
  color:var(--ink-2)!important;border-radius:99px!important;font-size:9.8px!important;
}
#w8-examples button:hover{border-color:var(--green-line)!important;background:var(--green-soft)!important;color:var(--green-dark)!important}
#w8-composer{
  margin:10px 18px 0!important;padding:6px 6px 6px 14px!important;gap:8px!important;
  align-items:center!important;display:flex!important;flex-wrap:nowrap!important;
  box-sizing:border-box!important;overflow:hidden!important;
  background:rgba(255,255,255,.96)!important;border:1px solid var(--line-strong)!important;
  border-radius:18px!important;box-shadow:0 8px 24px rgba(30,55,60,.07),0 1px 2px rgba(30,55,60,.04);
  transition:border-color .18s ease,box-shadow .18s ease;
}
#w8-composer > *{min-width:0!important}
#w8-composer:focus-within{
  border-color:var(--green)!important;
  box-shadow:0 0 0 3px rgba(47,111,98,.09),0 10px 26px rgba(30,55,60,.08);
}
#w8-input{flex:1 1 0%!important;width:0!important;min-width:0!important;border:0!important;border-radius:0!important;background:transparent!important;padding:0!important;box-shadow:none!important}
#w8-input label{display:block!important;width:100%!important;background:transparent!important;border:0!important;border-radius:0!important;padding:0!important;min-height:44px!important;box-shadow:none!important}
#w8-input textarea{
  font-size:12.5px!important;line-height:1.55!important;min-height:44px!important;
  padding:9px 0 8px!important;color:var(--ink)!important;background:transparent!important;
  overflow:hidden!important;border:0!important;box-shadow:none!important;
}
#w8-input textarea::placeholder{color:var(--faint)!important}
#w8-input textarea:focus{outline:0!important;box-shadow:none!important}
#w8-send{
  flex:0 0 36px!important;width:36px!important;height:36px!important;
  min-width:36px!important;min-height:36px!important;max-height:36px!important;
  align-self:center!important;position:relative!important;z-index:1!important;
  box-sizing:border-box!important;padding:0!important;margin:0!important;white-space:nowrap!important;
  border-radius:11px!important;background:var(--green)!important;color:#fff!important;
  border:0!important;font-size:17px!important;font-weight:600!important;line-height:1!important;
  box-shadow:0 4px 10px rgba(47,111,98,.18);
}
#w8-send:hover{background:var(--green-dark)!important}
#w8-send[disabled]{background:var(--green-soft)!important;color:var(--green)!important;border:1px solid var(--green-line)!important;opacity:1!important;box-shadow:none!important}
.w8-composer-meta{
  display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;
  padding:4px 24px 10px;font-size:8.8px;color:var(--faint);
}
.w8-composer-meta .w8-privacy{color:var(--red)}

/* ---------------- side panel ---------------- */
#w8-side-header{padding:13px 16px 9px!important;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#fbfdfd,#f6f9fa);flex:0 0 auto}
.w8-side-kicker{font-size:8.8px;letter-spacing:.13em;text-transform:uppercase;color:var(--faint);font-weight:700}
.w8-side-badges{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}
.w8-side-header h3{font-family:var(--serif);font-size:17px;font-weight:700;line-height:1.35;margin:6px 0 2px;color:var(--ink)}
.w8-side-header p{font-size:10.2px;line-height:1.5;color:var(--muted);margin:0}
#w8-tabs{padding:0 10px!important;flex:0 0 auto}
#w8-tabs .tab-nav{gap:2px!important;border-bottom:1px solid var(--line)!important;background:#fbfdfd!important;padding:0!important}
#w8-tabs button[role="tab"]{font-size:10.5px!important;font-weight:600!important;color:var(--muted)!important;padding:9px 8px!important}
#w8-tabs button[role="tab"][aria-selected="true"]{color:var(--green-dark)!important;border-bottom-color:var(--green)!important}
#w8-filters{padding:9px 12px 5px!important;gap:6px!important;align-items:center!important;flex:0 0 auto;display:flex!important;flex-wrap:nowrap!important}
#w8-filter{flex:1 1 auto!important;min-width:160px!important;background:#fff!important;border:1px solid var(--line)!important;border-radius:7px!important;min-height:32px!important;padding:1px 4px!important}
#w8-filter > div,#w8-filter .wrap,#w8-filter .wrap-inner,#w8-filter .secondary-wrap,#w8-filter .reference{width:100%!important;min-width:0!important}
#w8-filter input{width:100%!important;min-width:0!important;height:30px!important;font-size:10px!important;color:var(--ink)!important;padding:4px 5px!important}
#w8-filter input{font-size:10px!important;min-height:30px!important}
#w8-show-na{flex:0 0 auto!important;width:auto!important;min-width:108px!important;padding:0!important;background:transparent!important;border:0!important;font-size:9.5px!important;color:var(--muted)!important;overflow:visible!important}
#w8-show-na .checkbox-container{padding:0!important;background:transparent!important;gap:4px!important}
#w8-show-na .label-text{font-size:9.3px!important;color:var(--muted)!important}
#w8-checklist,#w8-citations,#w8-debug{padding:6px 12px 20px!important;overflow-y:auto!important}
#w8-checklist .block,#w8-citations .block,#w8-debug .block{padding:0!important}

/* ---------------- shared HTML components ---------------- */
.w8-badge{
  display:inline-flex;align-items:center;gap:5px;padding:3px 7px;border-radius:99px;
  border:1px solid transparent;font-size:9.2px;font-weight:700;line-height:1.15;white-space:nowrap;
}
.w8-dot{width:5px;height:5px;border-radius:50%;background:currentColor;flex:0 0 auto}
.w8-badge-success,.w8-badge-answered{color:var(--green);background:var(--green-soft);border-color:var(--green-line)}
.w8-badge-warn{color:var(--amber);background:var(--amber-soft);border-color:var(--amber-line)}
.w8-badge-info{color:var(--blue);background:var(--blue-soft);border-color:var(--blue-line)}
.w8-badge-abstain{color:var(--red);background:var(--red-soft);border-color:var(--red-line)}
.w8-badge-neutral{color:var(--muted);background:var(--gray-soft);border-color:var(--gray-line)}
.w8-badge-closed{color:var(--red);background:#fdf0ec;border-color:var(--red-line)}
.w8-cl-summary{border:1px solid var(--line);background:#fff;border-radius:10px;padding:10px;margin-bottom:9px}
.w8-cl-sum-top{display:flex;justify-content:space-between;gap:8px;align-items:baseline;margin-bottom:7px}
.w8-cl-sum-top strong{font-size:10.5px;color:var(--ink-2)}
.w8-cl-sum-top span{font-size:9px;color:var(--faint)}
.w8-sum-bar{display:flex;height:7px;border-radius:99px;overflow:hidden;background:var(--gray-soft);gap:1px}
.w8-sum-bar i{display:block;height:100%}
.w8-sum-closed{background:#b65a4d}.w8-sum-blocked{background:#c06a55}.w8-sum-conditional{background:#c6973c}
.w8-sum-available{background:#3d8a68}.w8-sum-done{background:#5c86a8}
.w8-sum-legend{display:flex;gap:9px;flex-wrap:wrap;margin-top:6px;font-size:8.8px;color:var(--muted)}
.w8-sum-legend span{display:inline-flex;align-items:center;gap:3px}
.w8-dot-closed{width:6px;height:6px;border-radius:50%;background:#b65a4d;display:block}
.w8-dot-blocked{width:6px;height:6px;border-radius:50%;background:#c06a55;display:block}
.w8-dot-conditional{width:6px;height:6px;border-radius:50%;background:#c6973c;display:block}
.w8-dot-available{width:6px;height:6px;border-radius:50%;background:#3d8a68;display:block}
.w8-dot-done{width:6px;height:6px;border-radius:50%;background:#5c86a8;display:block}
.w8-dot-na{width:6px;height:6px;border-radius:50%;background:#c8d1d5;display:block}
.w8-step{position:relative;margin-bottom:8px;padding:11px 11px 9px;background:#fff;border:1px solid var(--line);border-radius:10px}
.w8-step-card-closed{border-left:3px solid #b65a4d;background:linear-gradient(90deg,#fbf5f3,#fff 45%)}
.w8-step-card-conditional{border-left:3px solid #c6973c}
.w8-step-card-available{border-left:3px solid #3d8a68}
.w8-step-card-blocked{border-left:3px solid #c06a55}
.w8-step-card-not_applicable{opacity:.65}
.w8-step-head{display:flex;gap:6px;align-items:flex-start}
.w8-step-index{font-family:var(--mono);font-size:8px;color:var(--faint);padding-top:4px}
.w8-step-title{flex:1;margin:0;font-family:var(--serif);font-size:13px;font-weight:700;line-height:1.45;color:var(--ink)}
.w8-step-status{flex:0 0 auto}
.w8-step-badge{font-size:8.5px;padding:2px 6px;border-radius:99px;border:1px solid;font-weight:700;white-space:nowrap}
.w8-step-closed{color:var(--red);background:var(--red-soft);border-color:var(--red-line)}
.w8-step-conditional{color:var(--amber);background:var(--amber-soft);border-color:var(--amber-line)}
.w8-step-available{color:var(--green);background:var(--green-soft);border-color:var(--green-line)}
.w8-step-blocked{color:var(--red);background:var(--red-soft);border-color:var(--red-line)}
.w8-step-done{color:var(--blue);background:var(--blue-soft);border-color:var(--blue-line)}
.w8-step-na{color:var(--muted);background:var(--gray-soft);border-color:var(--gray-line)}
.w8-step-desc{margin:5px 0 7px;font-size:10.5px;line-height:1.6;color:var(--ink-2)}
.w8-step-meta{display:flex;flex-direction:column;gap:3px}
.w8-meta-line{display:flex;gap:5px;font-size:9.4px;line-height:1.5;color:var(--muted)}
.w8-meta-line strong{color:var(--ink-2);font-weight:600}
.w8-meta-closed{color:var(--red)}
.w8-materials{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}
.w8-material{font-size:8.8px;padding:2px 5px;border-radius:5px;background:var(--surface-2);border:1px solid var(--line);color:var(--ink-2)}
.w8-status-note{margin-top:6px;padding:5px 7px;border-radius:6px;background:var(--surface-2);border:1px solid var(--line);font-size:9.2px;line-height:1.5;color:var(--ink-2)}
.w8-status-closed,.w8-status-blocked{background:var(--red-soft);border-color:var(--red-line);color:var(--red)}
.w8-step-cites{margin-top:7px;padding-top:6px;border-top:1px dashed var(--line)}
.w8-step-cites summary{cursor:pointer;font-size:9px;color:var(--blue)}
.w8-cite-links{display:flex;flex-direction:column;gap:3px;margin-top:5px}
.w8-cite-links code{font-family:var(--mono);font-size:8.6px;color:var(--ink-2);word-break:break-all}
.w8-note{font-size:8.8px;line-height:1.5;color:var(--faint);margin:5px 0 0}
.w8-empty{padding:30px 14px;text-align:center;border:1px dashed var(--line-strong);border-radius:10px;background:rgba(255,255,255,.5);margin:4px 0}
.w8-empty-icon{width:32px;height:32px;border-radius:9px;background:var(--surface-2);border:1px solid var(--line);display:grid;place-items:center;margin:0 auto 8px;color:var(--faint);font-size:16px}
.w8-empty h3{margin:0 0 3px;font-size:10.5px;color:var(--ink-2)}
.w8-empty p{margin:0;font-size:9.4px;line-height:1.6;color:var(--faint)}
.w8-missing-list{display:flex;gap:4px;flex-wrap:wrap;justify-content:center;margin-top:8px}
.w8-missing-list span{font-size:8.6px;padding:2px 6px;border-radius:99px;background:var(--blue-soft);border:1px solid var(--blue-line);color:var(--blue)}
.w8-claim-map{border:1px solid var(--line);background:#fff;border-radius:9px;padding:9px;margin-bottom:9px}
.w8-claim-map h4{margin:0 0 5px;font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.w8-claim-map ol{margin:0;padding-left:16px}
.w8-claim-map li{font-size:9.6px;line-height:1.6;color:var(--ink-2);margin-bottom:3px}
.w8-cite-chip{display:inline-block;margin-left:2px;padding:0 4px;border:1px solid var(--green-line);background:var(--green-soft);color:var(--green-dark);border-radius:4px;font-size:8.4px;font-weight:600;text-decoration:none!important}
.w8-cite-note{padding:7px 8px;border:1px solid var(--green-line);background:var(--green-soft);border-radius:7px;font-size:9px;line-height:1.5;color:var(--green-dark);margin-bottom:8px}
.w8-cite-note-amber{background:var(--amber-soft);border-color:var(--amber-line);color:var(--amber)}
.w8-cite-group-title{display:flex;align-items:baseline;gap:6px;margin:10px 0 6px;font-size:8.8px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:700}
.w8-cite-group-title span{margin-left:auto;font-family:var(--mono);font-size:8.2px;color:var(--faint)}
.w8-cite-card{margin-bottom:8px;padding:10px;background:#fff;border:1px solid var(--line);border-radius:9px}
.w8-cite-head{display:flex;gap:5px;align-items:flex-start;flex-wrap:wrap}
.w8-cite-title{flex:1;min-width:140px;margin:0;font-family:var(--serif);font-size:12.4px;font-weight:600;line-height:1.5;color:var(--ink)}
.w8-cite-tags{display:flex;gap:3px;flex-wrap:wrap;flex:0 0 auto}
.w8-cite-tag{font-size:8px;padding:1px 4px;border-radius:4px;border:1px solid var(--line);background:var(--surface-2);color:var(--muted);white-space:nowrap}
.w8-cite-tag-low{color:var(--amber);background:var(--amber-soft);border-color:var(--amber-line)}
.w8-quote{margin:7px 0 6px;padding:6px 8px;border-left:2px solid var(--green-line);background:var(--surface-2);border-radius:0 5px 5px 0;font-size:10px;line-height:1.6;color:var(--ink-2)}
.w8-quote-empty{color:var(--faint);font-style:italic;border-left-color:var(--line-strong)}
.w8-cite-foot{display:flex;gap:6px;flex-wrap:wrap;font-size:8.8px;color:var(--faint)}
.w8-cite-foot a{color:var(--blue);text-decoration:none}
.w8-debug-banner{padding:7px 8px;border:1px solid var(--gray-line);background:var(--gray-soft);border-radius:7px;font-size:8.8px;line-height:1.5;color:var(--muted);margin-bottom:8px}
.w8-debug-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-bottom:8px}
.w8-debug-grid div{padding:7px 8px;border:1px solid var(--line);background:#fff;border-radius:7px;display:flex;flex-direction:column;gap:2px}
.w8-debug-grid span{font-size:8px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em}
.w8-debug-grid b{font-size:9.5px;color:var(--ink-2);font-weight:650;word-break:break-word}
.w8-debug-section{margin-bottom:5px;border:1px solid var(--line);border-radius:7px;background:#fff;overflow:hidden}
.w8-debug-section summary{list-style:none;cursor:pointer;padding:7px 8px;background:var(--surface-2);font-size:9.2px;font-weight:650;color:var(--ink-2);display:flex;justify-content:space-between}
.w8-debug-section summary::-webkit-details-marker{display:none}
.w8-debug-body{padding:7px 8px}
.w8-kv{display:grid;grid-template-columns:88px minmax(0,1fr);gap:3px 7px;margin:0;font-size:8.8px;line-height:1.5}
.w8-kv dt{color:var(--faint)}
.w8-kv dd{margin:0;color:var(--ink-2);word-break:break-word}
.w8-raw{margin-top:7px;border:1px solid var(--line);border-radius:7px;background:var(--surface-2)}
.w8-raw summary{padding:7px 8px;font-size:8.8px;color:var(--muted);cursor:pointer}
.w8-raw pre{margin:0;padding:7px 8px;border-top:1px solid var(--line);font-family:var(--mono);font-size:8px;line-height:1.5;color:var(--ink-2);max-height:220px;overflow:auto;white-space:pre-wrap}

#w8-history-toggle{display:none!important}
.w8-history-close{display:none;margin-left:auto;width:24px;height:24px;border:1px solid var(--line);background:#fff;color:var(--muted);border-radius:6px;font-size:14px;line-height:1;align-items:center;justify-content:center;padding:0;flex:0 0 auto}
@media (max-width: 1100px){
  #w8-root{flex-direction:column!important;flex-wrap:nowrap!important;height:100dvh!important;min-height:0;padding:0!important}
  #w8-chat{order:1;flex:1 1 auto!important;width:100%!important;max-width:100%!important;height:auto!important;min-height:0!important;border-radius:0;border-left:0;border-right:0;transition:flex-basis .22s ease}
  #w8-side{
    order:2;flex:0 0 0!important;width:100%!important;max-width:100%!important;min-width:0!important;
    height:0!important;max-height:0!important;min-height:0!important;
    padding:0!important;border:0!important;border-radius:16px 16px 0 0;
    overflow:hidden!important;opacity:0;pointer-events:none;
    box-shadow:none;transition:flex-basis .22s ease,height .22s ease,opacity .16s ease;
  }
  #w8-side.mobile-open{
    flex:0 0 40dvh!important;height:40dvh!important;max-height:40dvh!important;
    border-top:1px solid var(--line)!important;
    box-shadow:0 -12px 35px rgba(20,35,45,.12);overflow-y:auto!important;
    opacity:1;pointer-events:auto;
    animation:w8-sheet-up .24s ease;
  }
  #w8-history{
    position:fixed!important;top:0;bottom:0;left:0;width:284px;max-width:88vw;
    height:100%!important;max-height:none!important;z-index:120;
    transform:translateX(-102%);transition:transform .22s ease;
    border-radius:0 16px 16px 0;border-right:1px solid var(--line);
    box-shadow:18px 0 50px rgba(20,35,45,.18);
    padding:14px 13px!important;gap:9px!important;
  }
  #w8-history.mobile-open{transform:none}
  #w8-history-toggle{
    display:flex!important;width:30px!important;height:30px!important;
    min-width:30px!important;min-height:30px!important;padding:0!important;
    font-size:16px!important;border:1px solid var(--line)!important;
    background:#fff!important;color:var(--ink-2)!important;border-radius:7px!important;
  }
  .w8-history-close{display:flex!important;width:24px!important;height:24px!important}
  #w8-chat-head{padding:9px 12px!important;gap:6px!important}
  #w8-chatbot{padding:6px 12px!important}
  #w8-status,#w8-clarify-prompt{padding-left:12px!important;padding-right:12px!important}
  #w8-clarify-row,#w8-examples{padding-left:12px!important;padding-right:12px!important}
  #w8-composer{margin:6px 10px 0!important;padding:5px 6px 5px 12px!important;border-radius:15px!important}
  #w8-send{flex-basis:34px!important;width:34px!important;height:34px!important;min-width:34px!important;min-height:34px!important;max-height:34px!important;border-radius:10px!important}
  .w8-progress-stepper{gap:8px;padding:7px 10px}
  .w8-composer-meta{padding-left:12px!important;padding-right:12px!important}
  .w8-conversation-badges .w8-profile-note{display:none}
  .w8-conversation-title h2{font-size:14px}
  #w8-side-header{padding:10px 14px 7px!important}
  #w8-tabs{padding:0 6px!important}
  #w8-filters{padding:6px 10px 4px!important}
  #w8-checklist,#w8-citations,#w8-debug{padding:4px 10px 14px!important}
  .w8-side-header h3{font-size:16px}
}
@keyframes w8-sheet-up{from{transform:translateY(16px);opacity:.55}to{transform:translateY(0);opacity:1}}
"""

/* Discord Badge Maximizer — local web client.
 * All credentials stay in the Python process memory; this file never stores them.
 */
(() => {
  "use strict";

  const APP_KEY = String(window.__APP_KEY__ || "");
  const PUBLIC_MODE = Boolean(window.__PUBLIC_MODE__);
  const IDLE_FOOTER = PUBLIC_MODE ? "متصل بخادمك البعيد" : "متصل محلياً فقط";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const dom = {
    loginView: $("#loginView"),
    loginCard: $(".login-card"),
    tokenInput: $("#tokenInput"),
    cookieInput: $("#cookieInput"),
    openLoginBtn: $("#openLoginBtn"),
    continueBtn: $("#continueBtn"),
    appView: $("#appView"),
    topAvatar: $("#topAvatar"),
    topName: $("#topName"),
    topTag: $("#topTag"),
    credBtn: $("#credBtn"),
    logoutBtn: $("#logoutBtn"),
    lockBtn: $("#lockBtn"),
    profileBanner: $("#profileBanner"),
    profileAvatar: $("#profileAvatar"),
    profileDisplayName: $("#profileDisplayName"),
    profileUsername: $("#profileUsername"),
    profileBadges: $("#profileBadges"),
    profileBio: $("#profileBio"),
    profileCreated: $("#profileCreated"),
    connectedWrap: $("#connectedWrap"),
    connectedList: $("#connectedList"),
    statId: $("#statId"),
    statUsername: $("#statUsername"),
    statBadges: $("#statBadges"),
    statPremium: $("#statPremium"),
    badgeList: $("#badgeList"),
    badgeCount: $("#badgeCount"),
    refreshProfile: $("#refreshProfile"),
    profileLoading: $("#profileLoading"),
    gamesStatus: $("#gamesStatus"),
    gamesCount: $("#gamesCount"),
    gamesHours: $("#gamesHours"),
    gamesSummary: $("#gamesSummary"),
    gamesProgress: $("#gamesProgress"),
    gamesBar: $("#gamesBar"),
    claimBtn: $("#claimBtn"),
    gamesLog: $("#gamesLog"),
    gamesCookieHint: $("#gamesCookieHint"),
    streamStatus: $("#streamStatus"),
    guildId: $("#guildId"),
    channelId: $("#channelId"),
    streamStart: $("#streamStart"),
    streamStop: $("#streamStop"),
    liveBanner: $("#liveBanner"),
    liveTimer: $("#liveTimer"),
    streamLog: $("#streamLog"),
    streamCookieHint: $("#streamCookieHint"),
    altTokenInput: $("#altTokenInput"),
    altSave: $("#altSave"),
    altClear: $("#altClear"),
    viewerStatus: $("#viewerStatus"),
    viewerTimer: $("#viewerTimer"),
    viewerAccount: $("#viewerAccount"),
    viewerAvatar: $("#viewerAvatar"),
    viewerName: $("#viewerName"),
    viewerTag: $("#viewerTag"),
    viewerHint: $("#viewerHint"),
    footerState: $("#footerState"),
    privacyNote: $("#privacyNote"),
    credModal: $("#credModal"),
    modalToken: $("#modalToken"),
    modalCookie: $("#modalCookie"),
    credSave: $("#credSave"),
    toasts: $("#toasts"),
  };

  const state = {
    auth: false,
    snapshot: null,
    profile: null,
    profileLoading: false,
    lastLogId: 0,
    cutoffs: { games: 0, stream: 0 },
    claimToastKey: "",
    streamWasRunning: false,
    viewerWasRunning: false,
    pingsFailed: 0,
  };

  if (!APP_KEY || APP_KEY === "__APP_KEY__") {
    document.body.innerHTML =
      '<p style="padding:40px;font-family:system-ui;color:#eee">تعذر تحميل مفتاح التطبيق. شغّل التطبيق من الملف التنفيذي.</p>';
    return;
  }

  /* ------------------------------------------------------------------ helpers */
  async function api(path, { method = "GET", body } = {}) {
    const headers = { "X-App-Key": APP_KEY };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
      });
    } catch {
      throw new Error(
        PUBLIC_MODE
          ? "تعذر الاتصال بالخادم — تحقق من الشبكة ثم أعد المحاولة."
          : "تعذر الاتصال بالخادم المحلي — أعد فتح التطبيق من الملف التنفيذي."
      );
    }
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (response.status === 401 && PUBLIC_MODE) {
      window.location.href = "/login";
      throw new Error("انتهت جلسة اللوحة — أعد تسجيل الدخول.");
    }
    if (response.status === 403 && payload.error === "invalid app key") {
      throw new Error(
        PUBLIC_MODE
          ? "انتهت صلاحية الصفحة — أعد تحميلها من الخادم."
          : "انتهت الجلسة المحلية — أعد فتح التطبيق من الملف التنفيذي."
      );
    }
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  let lastToast = { message: "", at: 0 };

  function toast(message, kind = "info", duration = 4200) {
    const now = Date.now();
    if (message === lastToast.message && now - lastToast.at < 2800) return;
    lastToast = { message, at: now };
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    dom.toasts.appendChild(node);
    while (dom.toasts.children.length > 4) dom.toasts.removeChild(dom.toasts.firstElementChild);
    setTimeout(() => {
      node.style.opacity = "0";
      node.style.transform = "translateY(-6px)";
      node.style.transition = "opacity .25s ease, transform .25s ease";
      setTimeout(() => node.remove(), 260);
    }, duration);
  }

  function shake(node) {
    if (!node) return;
    node.classList.remove("shake");
    void node.offsetWidth;
    node.classList.add("shake");
  }

  function formatTimestamp(ms) {
    if (!ms) return "غير معروف";
    try {
      return new Intl.DateTimeFormat("ar", {
        day: "numeric",
        month: "long",
        year: "numeric",
      }).format(new Date(ms));
    } catch {
      return new Date(ms).toLocaleDateString();
    }
  }

  function formatHours(value) {
    return Number(value).toLocaleString("en-US", { maximumFractionDigits: 0 });
  }

  function accentGradient(color) {
    if (!color) return "";
    const hex = `#${Number(color).toString(16).padStart(6, "0").slice(-6)}`;
    return `linear-gradient(135deg, ${hex}, #1b1d24)`;
  }

  /* ------------------------------------------------------------------- badges */
  const gradient = (id, from, to) =>
    `<defs><linearGradient id="${id}" x1="3" y1="3" x2="21" y2="21" gradientUnits="userSpaceOnUse">` +
    `<stop stop-color="${from}"/><stop offset="1" stop-color="${to}"/></linearGradient></defs>`;

  const shieldIcon = (id, from, to, inner = "") =>
    `<svg viewBox="0 0 24 24" fill="none">${gradient(id, from, to)}` +
    `<path d="M12 2.6l7.6 2.9v6c0 4.7-3.2 8.6-7.6 10-4.4-1.4-7.6-5.3-7.6-10v-6L12 2.6z" fill="url(#${id})"/>` +
    inner +
    "</svg>";

  const houseIcon = (id, from, to) =>
    `<svg viewBox="0 0 24 24" fill="none">${gradient(id, from, to)}` +
    `<path d="M12 2.8L2.9 9.7h2.3v9.1h13.6V9.7h2.3L12 2.8z" fill="url(#${id})"/>` +
    '<path d="M12 7.4l1.8 3.7 3.7 1.8-3.7 1.8L12 18.4l-1.8-3.7-3.7-1.8 3.7-1.8L12 7.4z" fill="#14151a" opacity=".48"/>' +
    "</svg>";

  const starIcon = (id, from, to) =>
    `<svg viewBox="0 0 24 24" fill="none">${gradient(id, from, to)}` +
    `<path d="M12 2.4l2.8 6.8 7.1 2.8-7.1 2.8L12 21.6l-2.8-6.8L2.1 12l7.1-2.8z" fill="url(#${id})"/>` +
    "</svg>";

  const checkIcon = (id, from, to) =>
    `<svg viewBox="0 0 24 24" fill="none">${gradient(id, from, to)}` +
    `<circle cx="12" cy="12" r="9.4" fill="url(#${id})"/>` +
    '<path d="M8.2 12.3l2.5 2.6 5.2-5.5" stroke="#0d1712" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/>' +
    "</svg>";

  const bugIcon = (id, from, to) =>
    `<svg viewBox="0 0 24 24" fill="none">${gradient(id, from, to)}` +
    `<circle cx="12" cy="13.2" r="5.4" fill="url(#${id})"/>` +
    '<path d="M12 5.6v2.4M9 6.9l1.5 2.1M15 6.9l-1.5 2.1M5.8 12.4H3.2M20.8 12.4h-2.6M6.8 17.2l-2 2.2M17.2 17.2l2 2.2" stroke="#0f1a14" stroke-width="1.8" stroke-linecap="round"/>' +
    "</svg>";

  const gemIcon = (id, from, to) =>
    `<svg viewBox="0 0 24 24" fill="none">${gradient(id, from, to)}` +
    `<path d="M7.6 3.2h8.8l3.9 5.9-8.3 11.7L3.7 9.1z" fill="url(#${id})"/>` +
    '<path d="M4 9.1h16M12 3.2l-3 5.9 3 11.7 3-11.7z" stroke="#14151a" stroke-opacity=".42" stroke-width="1.1"/>' +
    "</svg>";

  const terminalIcon = (id, from, to) =>
    `<svg viewBox="0 0 24 24" fill="none">${gradient(id, from, to)}` +
    `<rect x="2.6" y="4.7" width="18.8" height="14.6" rx="4.2" fill="url(#${id})"/>` +
    '<path d="M7.3 9.4l3.1 2.6-3.1 2.6M12.7 15.3h4.2" stroke="#0d1218" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>' +
    "</svg>";

  const nitroIcon = (id, from, to) =>
    `<svg viewBox="0 0 24 24" fill="none">${gradient(id, from, to)}` +
    `<path d="M12 2.5l2.4 5.4 5.9.8-4.3 4.1 1.1 5.9L12 15.9l-5.1 2.8 1.1-5.9-4.3-4.1 5.9-.8z" fill="url(#${id})"/>` +
    "</svg>";

  const orbIcon = (id, from, to) =>
    `<svg viewBox="0 0 24 24" fill="none">${gradient(id, from, to)}` +
    `<circle cx="12" cy="12" r="9.3" fill="url(#${id})"/>` +
    '<circle cx="8.8" cy="9" r="3.2" fill="#ffffff" opacity=".32"/>' +
    '<circle cx="15.6" cy="15.6" r="1.5" fill="#ffffff" opacity=".24"/>' +
    "</svg>";

  const hatIcon = (id, from, to) =>
    `<svg viewBox="0 0 24 24" fill="none">${gradient(id, from, to)}` +
    `<path d="M12 3.2c-4 0-7.2 3.8-7.2 8.3 0 1.7 1 2.8 2.5 2.8 1.3 0 2.3-1 2.3-2.3 0-.9-.4-1.4-.4-2.1 0-1.4 1.3-2.6 2.8-2.6s2.8 1.2 2.8 2.6c0 .7-.4 1.2-.4 2.1 0 1.3 1 2.3 2.3 2.3 1.5 0 2.5-1.1 2.5-2.8 0-4.5-3.2-8.3-7.2-8.3z" fill="url(#${id})"/>` +
    '<circle cx="7.3" cy="12.2" r="1.4" fill="#14151a"/><circle cx="16.7" cy="12.2" r="1.4" fill="#14151a"/>' +
    "</svg>";

  function badgeIcon(badge) {
    const id = `bd-${Math.random().toString(36).slice(2, 9)}`;
    const raw = String(badge.id || "").toLowerCase();
    if (raw === "staff") {
      return shieldIcon(id, "#aab8ff", "#5865f2", '<path d="M12 6.4l1.5 3.5 3.6 1.5-3.6 1.5L12 16.4l-1.5-3.5L6.9 11.4l3.6-1.5z" fill="#14151a" opacity=".42"/>');
    }
    if (raw === "partner") return starIcon(id, "#b3c0ff", "#5865f2");
    if (raw === "certified_moderator") {
      return shieldIcon(id, "#9fc4ff", "#3c66d4", '<path d="M8.4 11.9l2.4 2.4 4.8-5" stroke="#101722" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/>');
    }
    if (raw.startsWith("hypesquad_house_")) {
      const house = raw.slice(-1);
      if (house === "2") return houseIcon(id, "#ffb0dc", "#d94f9f");
      if (house === "3") return houseIcon(id, "#9df0dc", "#2fae97");
      return houseIcon(id, "#c0acff", "#6c4fe0");
    }
    if (raw === "hypesquad" || raw.startsWith("hypesquad_events")) return houseIcon(id, "#ffe08a", "#d29a2c");
    if (raw.startsWith("bug_hunter")) {
      return raw.endsWith("2") ? bugIcon(id, "#ffdf94", "#d99a2b") : bugIcon(id, "#a8eec0", "#3fbf6a");
    }
    if (raw === "early_supporter") return gemIcon(id, "#ffb3cb", "#c2568c");
    if (raw === "early_verified_bot_developer" || raw === "verified_developer") {
      return terminalIcon(id, "#8fd7ff", "#2c86d8");
    }
    if (raw === "active_developer") return terminalIcon(id, "#8ef0b8", "#2fb37a");
    if (raw === "legacy_username") return checkIcon(id, "#c9d3e6", "#77839b");
    if (raw.startsWith("guild_booster")) return gemIcon(id, "#ffb4e2", "#b03ac0");
    if (raw.startsWith("premium")) return nitroIcon(id, "#ffa6e2", "#8b5cf6");
    if (raw === "quest_completed") return checkIcon(id, "#a6f4c6", "#3fbf76");
    if (raw === "orb_profile_badge") return orbIcon(id, "#d3e0ff", "#7f9cf5");
    if (raw.startsWith("april_fools")) return hatIcon(id, "#c0acff", "#e04f9d");
    return starIcon(id, "#edf1f9", "#a9b6cc");
  }

  const BADGE_NAMES = {
    staff: "Discord Staff",
    partner: "Partnered Server Owner",
    certified_moderator: "Moderator Programs Alumni",
    hypesquad: "HypeSquad Events",
    hypesquad_house_1: "HypeSquad Bravery",
    hypesquad_house_2: "HypeSquad Brilliance",
    hypesquad_house_3: "HypeSquad Balance",
    bug_hunter_level_1: "Bug Hunter",
    bug_hunter_level_2: "Bug Hunter Gold",
    early_supporter: "Early Supporter",
    verified_developer: "Verified Developer",
    early_verified_bot_developer: "Early Verified Developer",
    active_developer: "Active Developer",
    legacy_username: "Legacy Username",
    premium: "Nitro",
    quest_completed: "Quest Completed",
    orb_profile_badge: "Orb Profile Badge",
    april_fools_2026: "April Fools 2026",
  };

  function badgeName(badge) {
    const raw = String(badge.id || "");
    if (BADGE_NAMES[raw]) return BADGE_NAMES[raw];
    if (raw.startsWith("guild_booster_lvl")) {
      return `Server Booster — Lvl ${raw.replace(/\D/g, "") || "1"}`;
    }
    if (raw.startsWith("premium_tenure_")) {
      return `Nitro ${raw.replace("premium_tenure_", "")} Months`;
    }
    return badge.description || raw || "Badge";
  }

  /* ------------------------------------------------------------------ profile */
  function renderProfile(profile) {
    state.profile = profile;
    const display = profile.displayName || profile.username || "—";
    dom.profileDisplayName.textContent = display;
    dom.profileUsername.textContent = `@${profile.username || "unknown"}`;
    if (profile.avatarUrl) dom.profileAvatar.src = profile.avatarUrl;
    dom.profileBio.textContent = profile.bio || "لا توجد نبذة.";
    dom.profileCreated.textContent = profile.createdAt
      ? `عضو منذ ${formatTimestamp(profile.createdAt)}`
      : "غير معروف";

    if (profile.bannerUrl) {
      dom.profileBanner.style.backgroundImage = `url("${profile.bannerUrl}")`;
    } else if (profile.accentColor != null) {
      dom.profileBanner.style.backgroundImage = accentGradient(profile.accentColor);
    } else {
      dom.profileBanner.style.backgroundImage = "";
    }

    dom.profileBadges.replaceChildren();
    (profile.badges || []).forEach((badge) => {
      const item = document.createElement("span");
      item.className = "badge-icon";
      item.title = `${badgeName(badge)} — ${badge.description || badge.id}`;
      item.innerHTML = badgeIcon(badge);
      dom.profileBadges.appendChild(item);
    });

    dom.connectedList.replaceChildren();
    const connected = profile.connected || [];
    dom.connectedWrap.hidden = connected.length === 0;
    connected.forEach((entry) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      const label = document.createElement("span");
      label.textContent = entry.name || entry.type;
      chip.appendChild(label);
      if (entry.verified) {
        const mark = document.createElement("span");
        mark.innerHTML =
          '<svg viewBox="0 0 24 24" width="13" height="13" fill="none"><circle cx="12" cy="12" r="9" fill="#5865f2"/><path d="M8.4 12.2l2.4 2.4 4.8-5" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        chip.appendChild(mark);
      }
      dom.connectedList.appendChild(chip);
    });

    dom.statId.textContent = profile.id || "—";
    dom.statUsername.textContent = profile.username ? `@${profile.username}` : "—";
    dom.statBadges.textContent = `${(profile.badges || []).length}`;
    dom.statPremium.textContent = profile.premiumSince
      ? formatTimestamp(profile.premiumSince)
      : "لا يوجد";

    const allBadges = [...(profile.badges || []), ...(profile.guildBadges || [])];
    dom.badgeCount.textContent = String(allBadges.length);
    dom.badgeList.replaceChildren();
    if (!allBadges.length) {
      const empty = document.createElement("li");
      empty.className = "empty";
      empty.textContent = "لا توجد بادجات ظاهرة.";
      dom.badgeList.appendChild(empty);
    } else {
      allBadges.forEach((badge) => {
        const row = document.createElement("li");
        const icon = document.createElement("span");
        icon.className = "badge-icon";
        icon.innerHTML = badgeIcon(badge);
        const text = document.createElement("span");
        text.className = "badge-item-text";
        const title = document.createElement("strong");
        title.textContent = badgeName(badge);
        const subtitle = document.createElement("span");
        subtitle.textContent = badge.description || badge.id;
        text.append(title, subtitle);
        row.append(icon, text);
        dom.badgeList.appendChild(row);
      });
    }
  }

  async function loadProfile(force = false) {
    if (!state.auth) return;
    if (state.profileLoading) return;
    if (!force && state.profile) {
      renderProfile(state.profile);
      return;
    }
    state.profileLoading = true;
    dom.profileLoading.hidden = false;
    try {
      const { profile } = await api("/api/profile");
      renderProfile(profile);
    } catch (error) {
      toast(`تعذر جلب البروفايل: ${error.message}`, "error", 6000);
    } finally {
      state.profileLoading = false;
      dom.profileLoading.hidden = true;
    }
  }

  /* ---------------------------------------------------------------- navigation */
  function selectTab(tab) {
    $$("[data-tab]").forEach((button) => {
      const active = button.dataset.tab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    $$(".tab-panel").forEach((panel) => {
      const active = panel.id === `panel-${tab}`;
      panel.classList.toggle("active", active);
      panel.hidden = !active;
    });
    if (tab === "profile" && state.auth && !state.profile) loadProfile(true);
  }

  /* -------------------------------------------------------------------- login */
  function setLoginBusy(busy) {
    dom.continueBtn.disabled = busy;
    dom.continueBtn.textContent = busy ? "جاري التحقق…" : "تحقق ومتابعة";
  }

  function enterApp(account) {
    state.auth = true;
    dom.loginView.hidden = true;
    dom.appView.hidden = false;
    if (account) {
      dom.topName.textContent = account.displayName || account.username || "—";
      dom.topTag.textContent = account.username ? `@${account.username}` : "";
      if (account.avatarUrl) dom.topAvatar.src = account.avatarUrl;
    }
    dom.claimBtn.disabled = false;
    dom.streamStart.disabled = false;
    selectTab("profile");
  }

  function applyAltAccount(account) {
    dom.viewerAccount.hidden = !account;
    if (!account) {
      dom.viewerAvatar.removeAttribute("src");
      dom.viewerAvatar.hidden = true;
      dom.viewerName.textContent = "—";
      dom.viewerTag.textContent = "";
      return;
    }
    dom.viewerName.textContent = account.displayName || account.username || "—";
    dom.viewerTag.textContent = account.username ? `@${account.username}` : "";
    dom.viewerAvatar.hidden = !account.avatarUrl;
    if (account.avatarUrl) dom.viewerAvatar.src = account.avatarUrl;
  }

  function leaveApp() {
    state.auth = false;
    state.profile = null;
    state.profileLoading = false;
    state.snapshot = null;
    state.claimToastKey = "";
    state.streamWasRunning = false;
    state.viewerWasRunning = false;
    dom.appView.hidden = true;
    dom.loginView.hidden = false;
    dom.tokenInput.value = "";
    dom.cookieInput.value = "";
    dom.altTokenInput.value = "";
    dom.profileBadges.replaceChildren();
    dom.profileDisplayName.textContent = "—";
    dom.profileUsername.textContent = "—";
    dom.profileBio.textContent = "لا توجد نبذة.";
    dom.profileCreated.textContent = "—";
    dom.profileAvatar.removeAttribute("src");
    dom.profileBanner.style.backgroundImage = "";
    dom.connectedWrap.hidden = true;
    dom.badgeList.replaceChildren();
    dom.badgeCount.textContent = "0";
    dom.statId.textContent = "—";
    dom.statUsername.textContent = "—";
    dom.statBadges.textContent = "—";
    dom.statPremium.textContent = "—";
    dom.liveBanner.hidden = true;
    setStatus(dom.gamesStatus, "جاهز", "");
    setStatus(dom.streamStatus, "غير متصل", "");
    setStatus(dom.viewerStatus, "غير متصل", "");
    dom.viewerTimer.hidden = true;
    applyAltAccount(null);
    dom.viewerHint.hidden = false;
    dom.streamStart.textContent = "بدء البث";
    dom.claimBtn.disabled = false;
    dom.claimBtn.textContent = "بدء المطالبة";
    dom.streamStart.disabled = false;
    dom.streamStop.disabled = true;
  }

  async function submitLogin() {
    const token = dom.tokenInput.value.trim();
    const cookie = dom.cookieInput.value.trim();
    if (!token) {
      toast("الصق توكن الحساب أولاً.", "error");
      dom.tokenInput.focus();
      shake(dom.loginCard);
      return;
    }
    setLoginBusy(true);
    try {
      const { account } = await api("/api/token", { method: "POST", body: { token } });
      dom.tokenInput.value = "";
      let cookieSaved = false;
      if (cookie) {
        try {
          await api("/api/cookie", { method: "POST", body: { cookie } });
          cookieSaved = true;
          dom.cookieInput.value = "";
        } catch (error) {
          toast(`تم حفظ التوكن، لكن الكوكي رُفض: ${error.message}`, "warn", 6500);
        }
      }
      toast(`تم التحقق من الحساب: ${account.displayName}`, "ok");
      enterApp(account);
      await loadProfile(true);
      if (!cookieSaved) {
        toast("أضف كوكي cf_clearance من زر بيانات الدخول قبل المطالبة أو البث.", "warn", 6500);
      }
    } catch (error) {
      toast(error.message, "error", 7000);
      shake(dom.loginCard);
    } finally {
      setLoginBusy(false);
    }
  }

  async function pasteInto(input) {
    try {
      const text = await navigator.clipboard.readText();
      input.value = text.trim();
      input.focus();
      toast("تم اللصق من الحافظة.", "ok", 2200);
    } catch {
      toast("المتصفح منع الوصول للحافظة — استخدم Ctrl+V.", "warn");
      input.focus();
    }
  }

  /* --------------------------------------------------------------------- claim */
  function updateGamesSummary() {
    const countRaw = dom.gamesCount.value.trim();
    const hours = Number.parseFloat(dom.gamesHours.value) || 0;
    if (countRaw === "") {
      dom.gamesSummary.textContent = `كل الألعاب المتاحة × ${formatHours(hours)} ساعة.`;
      return;
    }
    const count = Number.parseInt(countRaw, 10) || 0;
    dom.gamesSummary.textContent =
      `${formatHours(count)} لعبة × ${formatHours(hours)} ساعة = ` +
      `${formatHours(count * hours)} ساعة إجمالاً.`;
  }

  async function startClaim() {
    const count = dom.gamesCount.value.trim() === "" ? 0 : Number.parseInt(dom.gamesCount.value, 10);
    const hours = Number.parseFloat(dom.gamesHours.value);
    if (!Number.isFinite(count) || count < 0) {
      toast("أدخل عدد ألعاب صحيحاً.", "error");
      shake(dom.gamesCount);
      return;
    }
    if (!Number.isFinite(hours) || hours <= 0) {
      toast("أدخل ساعات أكبر من صفر.", "error");
      shake(dom.gamesHours);
      return;
    }
    dom.claimBtn.disabled = true;
    try {
      await api("/api/claim", { method: "POST", body: { count, hours } });
      toast("بدأت المطالبة — تابع السجل أدناه.", "ok");
    } catch (error) {
      dom.claimBtn.disabled = false;
      toast(error.message, "error", 6000);
    }
  }

  /* -------------------------------------------------------------------- stream */
  async function startStream() {
    const guildId = dom.guildId.value.trim();
    const channelId = dom.channelId.value.trim();
    if (!guildId || !channelId) {
      toast("أدخل Server ID و Voice Channel ID.", "error");
      shake(!guildId ? dom.guildId : dom.channelId);
      return;
    }
    dom.streamStart.disabled = true;
    try {
      await api("/api/stream/start", {
        method: "POST",
        body: { guildId, channelId, withViewer: true },
      });
      const hasViewer = Boolean(state.snapshot && state.snapshot.altAuthenticated);
      toast(hasViewer ? "جاري بدء البث ودخول حساب المشاهدة…" : "جاري بدء البث…", "ok");
    } catch (error) {
      dom.streamStart.disabled = false;
      toast(error.message, "error", 6000);
    }
  }

  async function stopStream() {
    dom.streamStop.disabled = true;
    try {
      await api("/api/stream/stop", { method: "POST", body: {} });
      toast("جاري إيقاف البث وخروج المشاهد…", "warn");
    } catch (error) {
      dom.streamStop.disabled = false;
      toast(error.message, "error", 6000);
    }
  }

  async function saveAltCredentials() {
    const token = dom.altTokenInput.value.trim();
    if (!token) {
      toast("الصق توكن الحساب الثاني أولاً.", "error");
      shake(dom.altTokenInput);
      dom.altTokenInput.focus();
      return;
    }
    dom.altSave.disabled = true;
    try {
      const { account } = await api("/api/alt/token", { method: "POST", body: { token } });
      dom.altTokenInput.value = "";
      applyAltAccount(account);
      dom.viewerHint.hidden = true;
      toast(`تم تجهيز حساب المشاهدة: ${account.displayName}`, "ok");
      await pollState();
    } catch (error) {
      toast(error.message, "error", 7000);
      shake(dom.altTokenInput);
    } finally {
      dom.altSave.disabled = false;
    }
  }

  async function clearAltAccount() {
    const confirmed = window.confirm("سيتم مسح توكن الحساب الثاني من ذاكرة التطبيق. متابعة؟");
    if (!confirmed) return;
    dom.altClear.disabled = true;
    try {
      await api("/api/alt/clear", { method: "POST", body: {} });
      applyAltAccount(null);
      dom.viewerHint.hidden = false;
      toast("تم مسح حساب المشاهدة من الذاكرة.", "warn");
      await pollState();
    } catch (error) {
      toast(error.message, "error", 6000);
    } finally {
      dom.altClear.disabled = false;
    }
  }

  function renderTimer(node, session) {
    if (!session || !session.running || !session.startedAt) {
      node.hidden = node === dom.viewerTimer;
      if (node !== dom.viewerTimer) node.textContent = "00:00:00";
      return;
    }
    node.hidden = false;
    const total = Math.max(0, Math.floor(Date.now() / 1000 - session.startedAt));
    const hours = String(Math.floor(total / 3600)).padStart(2, "0");
    const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
    const seconds = String(total % 60).padStart(2, "0");
    node.textContent = `${hours}:${minutes}:${seconds}`;
  }

  function updateLiveTimer() {
    const snapshot = state.snapshot || {};
    renderTimer(dom.liveTimer, snapshot.stream);
    renderTimer(dom.viewerTimer, snapshot.viewer);
  }

  /* ---------------------------------------------------------------------- logs */
  function appendLogs(lines) {
    if (!lines.length) return;
    let maxId = state.lastLogId;
    ["games", "stream"].forEach((target) => {
      const consoleNode = target === "games" ? dom.gamesLog : dom.streamLog;
      const cutoff = state.cutoffs[target];
      const nearBottom =
        consoleNode.scrollTop + consoleNode.clientHeight >= consoleNode.scrollHeight - 48;
      lines.forEach((line) => {
        if (line.id <= cutoff) return;
        const row = document.createElement("div");
        row.className = `log-line ${line.level || "info"}`;
        const time = document.createElement("span");
        time.className = "log-time";
        time.textContent = line.time;
        const text = document.createElement("span");
        text.className = "log-text";
        text.textContent = line.text;
        row.append(time, text);
        consoleNode.appendChild(row);
      });
      while (consoleNode.children.length > 400) consoleNode.firstElementChild.remove();
      if (nearBottom) consoleNode.scrollTop = consoleNode.scrollHeight;
    });
    lines.forEach((line) => {
      if (line.id > maxId) maxId = line.id;
    });
    state.lastLogId = maxId;
  }

  async function pollLogs() {
    try {
      const { lines } = await api(`/api/logs?after=${state.lastLogId}`);
      appendLogs(lines || []);
    } catch {
      /* server may be shutting down */
    }
  }

  function clearLogView(target) {
    const consoleNode = target === "games" ? dom.gamesLog : dom.streamLog;
    consoleNode.replaceChildren();
    state.cutoffs[target] = state.lastLogId;
  }

  /* --------------------------------------------------------------------- state */
  function setStatus(node, text, kind) {
    node.textContent = text;
    node.className = `pill-status${kind ? ` ${kind}` : ""}`;
  }

  function applySnapshot(snapshot) {
    state.snapshot = snapshot;

    if (!snapshot.authenticated && state.auth) {
      leaveApp();
      toast("انتهت الجلسة — أعد إدخال بيانات الدخول.", "warn", 6000);
    }
    if (snapshot.authenticated && !state.auth) {
      enterApp(snapshot.account);
      loadProfile(true);
    }

    if (snapshot.account) {
      dom.topName.textContent = snapshot.account.displayName || "—";
      dom.topTag.textContent = snapshot.account.username ? `@${snapshot.account.username}` : "";
      if (snapshot.account.avatarUrl) dom.topAvatar.src = snapshot.account.avatarUrl;
    }

    const claim = snapshot.claim || {};
    dom.gamesCookieHint.hidden = Boolean(snapshot.hasCookie);
    dom.streamCookieHint.hidden = Boolean(snapshot.hasCookie);

    if (claim.running) {
      dom.claimBtn.disabled = true;
      dom.claimBtn.textContent = "جاري الإرسال…";
      dom.gamesProgress.hidden = false;
      dom.gamesBar.classList.add("indeterminate");
      dom.gamesBar.style.width = "";
      setStatus(dom.gamesStatus, "قيد التنفيذ", "busy");
    } else {
      dom.claimBtn.textContent = "بدء المطالبة";
      dom.claimBtn.disabled = !state.auth;
      dom.gamesBar.classList.remove("indeterminate");
      if (claim.done) {
        dom.gamesProgress.hidden = false;
        const ratio = claim.total > 0 ? clamp(claim.accepted / claim.total, 0, 1) : 0;
        dom.gamesBar.style.width = `${Math.round(ratio * 100)}%`;
        if (claim.error) {
          setStatus(dom.gamesStatus, "فشل", "error");
        } else {
          setStatus(dom.gamesStatus, `اكتمل ${claim.accepted}/${claim.total}`, "ok");
        }
        const key = `${claim.accepted}/${claim.total}/${claim.error}`;
        if (state.claimToastKey !== key) {
          state.claimToastKey = key;
          if (claim.error) toast(`فشلت المطالبة: ${claim.error}`, "error", 7000);
          else toast(`انتهت المطالبة: ${claim.accepted}/${claim.total} لعبة.`, "ok", 6000);
        }
      } else {
        dom.gamesProgress.hidden = true;
        dom.gamesBar.style.width = "0";
        setStatus(dom.gamesStatus, "جاهز", "");
      }
    }

    const stream = snapshot.stream || {};
    const live = Boolean(stream.running) && String(stream.status || "").includes("LIVE");
    dom.streamStart.disabled = !state.auth || Boolean(stream.running);
    dom.streamStop.disabled = !stream.running && !(snapshot.viewer || {}).running;
    dom.liveBanner.hidden = !live;
    if (stream.running) {
      setStatus(dom.streamStatus, stream.status || "متصل — LIVE", "live");
    } else {
      setStatus(dom.streamStatus, stream.status || "غير متصل", "");
    }
    if (state.streamWasRunning && !stream.running) {
      toast("توقف البث.", "warn");
    }
    state.streamWasRunning = Boolean(stream.running);

    const viewer = snapshot.viewer || {};
    dom.viewerHint.hidden = Boolean(snapshot.altAuthenticated);
    applyAltAccount(snapshot.altAccount || null);
    dom.streamStart.textContent = snapshot.altAuthenticated
      ? "بدء البث مع المشاهد"
      : "بدء البث";
    if (viewer.running) {
      const joined = String(viewer.status || "").includes("مشاهد");
      setStatus(dom.viewerStatus, viewer.status || "متصل — مشاهد", joined ? "ok" : "busy");
    } else {
      setStatus(dom.viewerStatus, "غير متصل", "");
    }
    if (state.viewerWasRunning && !viewer.running) {
      toast("خرج حساب المشاهدة من الروم.", "warn");
    }
    state.viewerWasRunning = Boolean(viewer.running);
    updateLiveTimer();
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  async function pollState() {
    try {
      const snapshot = await api("/api/state");
      applySnapshot(snapshot);
      dom.footerState.textContent = IDLE_FOOTER;
    } catch {
      dom.footerState.textContent = "انقطع الاتصال بالخادم";
    }
  }

  async function ping() {
    try {
      await api("/api/ping", { method: "POST" });
      state.pingsFailed = 0;
    } catch {
      state.pingsFailed += 1;
      if (state.pingsFailed >= 2) dom.footerState.textContent = "لا يستجيب الخادم";
    }
  }

  /* ---------------------------------------------------------------- credentials */
  function openCredModal() {
    dom.modalToken.value = "";
    dom.modalCookie.value = "";
    dom.credModal.hidden = false;
    dom.modalToken.focus();
  }

  function closeCredModal() {
    dom.credModal.hidden = true;
    dom.modalToken.value = "";
    dom.modalCookie.value = "";
  }

  async function saveCredModal() {
    const token = dom.modalToken.value.trim();
    const cookie = dom.modalCookie.value.trim();
    if (!token && !cookie) {
      toast("أدخل توكن أو كوكي جديد.", "warn");
      return;
    }
    dom.credSave.disabled = true;
    try {
      if (token) {
        const { account } = await api("/api/token", { method: "POST", body: { token } });
        toast(`تم تحديث الحساب: ${account.displayName}`, "ok");
        enterApp(account);
        await loadProfile(true);
      }
      if (cookie) {
        await api("/api/cookie", { method: "POST", body: { cookie } });
        toast("تم تحديث الكوكي في الذاكرة.", "ok");
      }
      closeCredModal();
      await pollState();
    } catch (error) {
      toast(error.message, "error", 6500);
    } finally {
      dom.credSave.disabled = false;
    }
  }

  async function logout() {
    const confirmed = window.confirm(
      "سيتم مسح التوكن والكوكي وبيانات الحساب الثاني من ذاكرة التطبيق وإيقاف البث. متابعة؟"
    );
    if (!confirmed) return;
    try {
      await api("/api/clear", { method: "POST", body: {} });
      leaveApp();
      toast("تم مسح بيانات الدخول من الذاكرة.", "warn");
    } catch (error) {
      toast(error.message, "error", 6000);
    }
  }

  async function lockPanel() {
    try {
      await api("/api/logout", { method: "POST", body: {} });
    } catch {
      /* the redirect below still ends the session view */
    }
    window.location.href = "/login";
  }

  /* ------------------------------------------------------------------- wiring */
  $$("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
  });

  $$("[data-paste]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = button.dataset.paste === "token" ? dom.tokenInput : dom.cookieInput;
      pasteInto(input);
    });
  });

  $$("[data-eye]").forEach((button) => {
    button.addEventListener("click", () => {
      const input =
        button.dataset.eye === "token"
          ? dom.tokenInput
          : button.dataset.eye === "altToken"
            ? dom.altTokenInput
            : dom.cookieInput;
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      button.setAttribute("aria-label", show ? "إخفاء القيمة" : "إظهار القيمة");
    });
  });

  dom.openLoginBtn.addEventListener("click", async () => {
    try {
      const result = await api("/api/open-login", { method: "POST", body: {} });
      if (PUBLIC_MODE && result.url) {
        window.open(result.url, "_blank", "noopener");
      }
      toast("تم فتح صفحة Discord الرسمية.", "ok", 5000);
    } catch (error) {
      toast(error.message, "error");
    }
  });

  dom.continueBtn.addEventListener("click", submitLogin);

  [dom.tokenInput, dom.cookieInput].forEach((input) => {
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") submitLogin();
    });
  });

  dom.refreshProfile.addEventListener("click", () => loadProfile(true));
  dom.claimBtn.addEventListener("click", startClaim);
  dom.streamStart.addEventListener("click", startStream);
  dom.streamStop.addEventListener("click", stopStream);
  dom.altSave.addEventListener("click", saveAltCredentials);
  dom.altClear.addEventListener("click", clearAltAccount);
  dom.altTokenInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveAltCredentials();
  });
  dom.gamesCount.addEventListener("input", updateGamesSummary);
  dom.gamesHours.addEventListener("input", updateGamesSummary);

  $$("[data-clear-log]").forEach((button) => {
    button.addEventListener("click", () => clearLogView(button.dataset.clearLog));
  });

  dom.credBtn.addEventListener("click", openCredModal);
  dom.credSave.addEventListener("click", saveCredModal);
  dom.logoutBtn.addEventListener("click", logout);
  dom.lockBtn.addEventListener("click", lockPanel);
  $$("[data-close-modal]").forEach((button) => button.addEventListener("click", closeCredModal));
  dom.credModal.addEventListener("click", (event) => {
    if (event.target === dom.credModal) closeCredModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !dom.credModal.hidden) closeCredModal();
  });

  window.addEventListener("pagehide", () => {
    if (PUBLIC_MODE) return;
    try {
      fetch("/api/quit", {
        method: "POST",
        headers: { "X-App-Key": APP_KEY },
        keepalive: true,
      });
    } catch {
      /* ignore */
    }
  });

  /* --------------------------------------------------------------------- boot */
  async function boot() {
    dom.lockBtn.hidden = !PUBLIC_MODE;
    dom.footerState.textContent = IDLE_FOOTER;
    if (PUBLIC_MODE && dom.privacyNote && dom.privacyNote.lastChild) {
      dom.privacyNote.lastChild.textContent =
        " يبقيان في ذاكرة خادمك فقط، ولا يُرسلان إلى أي جهة أخرى غير ديسكورد. ";
    }
    updateGamesSummary();
    try {
      const snapshot = await api("/api/state");
      applySnapshot(snapshot);
    } catch (error) {
      dom.footerState.textContent = "انقطع الاتصال بالخادم";
      toast(error.message, "error", 9000);
    }
    setInterval(pollState, 1500);
    setInterval(pollLogs, 1000);
    setInterval(updateLiveTimer, 1000);
    setInterval(ping, 5000);
    ping();
  }

  boot();
})();

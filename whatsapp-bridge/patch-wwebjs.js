/**
 * Patch whatsapp-web.js contact getters that crash on newer WhatsApp Web (LID/memoize errors).
 * Safe to run multiple times (postinstall / Docker build).
 */
const fs = require("fs");
const path = require("path");

const utilsPath = path.join(__dirname, "node_modules", "whatsapp-web.js", "src", "util", "Injected", "Utils.js");

if (!fs.existsSync(utilsPath)) {
  console.warn("[patch-wwebjs] whatsapp-web.js Utils.js not found — skip");
  process.exit(0);
}

let src = fs.readFileSync(utilsPath, "utf8");
const alreadyV1 = src.includes("PATCHED_CONTACT_GETTER_GUARD");
const alreadyV2 = src.includes("PATCHED_CONTACT_GETTER_V2");
const alreadyV3 = src.includes("PATCHED_SEND_MESSAGE_V3");

if (alreadyV2 && alreadyV3) {
  console.log("[patch-wwebjs] Already patched (v2 + v3)");
  process.exit(0);
}

const oldGetContact = `    window.WWebJS.getContact = async (contactId) => {
        const contactWid = window
            .require('WAWebWidFactory')
            .createWid(contactId);
        const contact = await window
            .require('WAWebCollections')
            .Contact.find(contactWid);
        if (contact.isBusiness || contact.isEnterprise) {
            const bizProfile = await window
                .require('WAWebCollections')
                .BusinessProfile.find(contactWid);
            bizProfile.profileOptions && (contact.businessProfile = bizProfile);
        }
        return window.WWebJS.getContactModel(contact);
    };`;

const newGetContact = `    window.WWebJS.getContact = async (contactId) => {
        /* PATCHED_CONTACT_GETTER_GUARD */
        const contactWid = window.require('WAWebWidFactory').createWid(contactId);
        let contact = window.require('WAWebCollections').Contact.get(contactWid);
        if (!contact) {
            try {
                contact = await window.require('WAWebCollections').Contact.find(contactWid);
            } catch {
                contact = null;
            }
        }
        if (!contact || !contact.id) return null;
        if (contact.isBusiness || contact.isEnterprise) {
            try {
                const bizProfile = await window
                    .require('WAWebCollections')
                    .BusinessProfile.find(contactWid);
                if (bizProfile?.profileOptions) contact.businessProfile = bizProfile;
            } catch {
                /* non-business or lookup failed */
            }
        }
        return window.WWebJS.getContactModel(contact);
    };`;

const oldGetContactModelStart = `    window.WWebJS.getContactModel = (contact) => {
        let res = contact.serialize();

        const wid = window
            .require('WAWebWidFactory')
            .createWidFromWidLike(contact.id);
        if (wid.isLid() && contact.phoneNumber) {
            res.id = contact.phoneNumber;
        }`;

const newGetContactModelStart = `    window.WWebJS.getContactModel = (contact) => {
        /* PATCHED_CONTACT_GETTER_GUARD */
        if (!contact || !contact.id) {
            return {
                id: { _serialized: '0@c.us', user: '0', server: 'c.us' },
                number: '',
                name: '',
                shortName: '',
                pushname: '',
                isMe: false,
                isUser: false,
                isGroup: false,
                isWAContact: false,
                isMyContact: false,
                isBusiness: false,
                isEnterprise: false,
                isBlocked: false,
            };
        }
        let res = contact.serialize();
        const wid = window.require('WAWebWidFactory').createWidFromWidLike(contact.id);
        if (wid?.isLid?.() && contact.phoneNumber) {
            res.id = contact.phoneNumber;
        }`;

if (!alreadyV1) {
  if (!src.includes(oldGetContact)) {
    console.warn("[patch-wwebjs] getContact block not found — library version may differ");
    process.exit(0);
  }
  src = src.replace(oldGetContact, newGetContact);
  if (src.includes(oldGetContactModelStart)) {
    src = src.replace(oldGetContactModelStart, newGetContactModelStart);
  } else {
    console.warn("[patch-wwebjs] getContactModel block not found — partial patch only");
  }
}

const unsafeContactMethods = `        const ContactMethods = window.require('WAWebContactGetters');
        res.isMe = ContactMethods.getIsMe(contact);
        res.isUser = ContactMethods.getIsUser(contact);
        res.isGroup = ContactMethods.getIsGroup(contact);
        res.isWAContact = ContactMethods.getIsWAContact(contact);
        res.userid = ContactMethods.getUserid(contact);
        res.verifiedName = ContactMethods.getVerifiedName(contact);
        res.verifiedLevel = ContactMethods.getVerifiedLevel(contact);
        res.statusMute = ContactMethods.getStatusMute(contact);
        res.name = ContactMethods.getName(contact);
        res.shortName = ContactMethods.getShortName(contact);
        res.pushname = ContactMethods.getPushname(contact);

        const { getIsMyContact } = window.require(
            'WAWebFrontendContactGetters',
        );
        res.isMyContact = getIsMyContact(contact);
        res.isEnterprise = ContactMethods.getIsEnterprise(contact);`;

const safeContactMethods = `        /* PATCHED_CONTACT_GETTER_V2 */
        const ContactMethods = window.require('WAWebContactGetters');
        const safeContactGet = (fn, fallback) => {
            try {
                if (!contact?.id) return fallback;
                return fn(contact);
            } catch {
                return fallback;
            }
        };
        res.isMe = safeContactGet(ContactMethods.getIsMe, false);
        res.isUser = safeContactGet(ContactMethods.getIsUser, false);
        res.isGroup = safeContactGet(ContactMethods.getIsGroup, false);
        res.isWAContact = safeContactGet(ContactMethods.getIsWAContact, false);
        res.userid = safeContactGet(ContactMethods.getUserid, '');
        res.verifiedName = safeContactGet(ContactMethods.getVerifiedName, null);
        res.verifiedLevel = safeContactGet(ContactMethods.getVerifiedLevel, null);
        res.statusMute = safeContactGet(ContactMethods.getStatusMute, false);
        res.name = safeContactGet(ContactMethods.getName, '');
        res.shortName = safeContactGet(ContactMethods.getShortName, '');
        res.pushname = safeContactGet(ContactMethods.getPushname, '');
        try {
            const { getIsMyContact } = window.require('WAWebFrontendContactGetters');
            res.isMyContact = safeContactGet(getIsMyContact, false);
        } catch {
            res.isMyContact = false;
        }
        res.isEnterprise = safeContactGet(ContactMethods.getIsEnterprise, false);`;

if (src.includes(unsafeContactMethods)) {
  src = src.replace(unsafeContactMethods, safeContactMethods);
} else if (!src.includes("PATCHED_CONTACT_GETTER_V2")) {
  console.warn("[patch-wwebjs] ContactMethods block not found — v2 patch skipped");
}

const unsafeSendMessageStart = `    window.WWebJS.sendMessage = async (chat, content, options = {}) => {
        const { getIsNewsletter, getIsBroadcast } =
            window.require('WAWebChatGetters');
        const isChannel = getIsNewsletter(chat);
        const isStatus = getIsBroadcast(chat);`;

const safeSendMessageStart = `    window.WWebJS.sendMessage = async (chat, content, options = {}) => {
        /* PATCHED_SEND_MESSAGE_V3 */
        let isChannel = false;
        let isStatus = false;
        try {
            const { getIsNewsletter, getIsBroadcast } =
                window.require('WAWebChatGetters');
            if (chat?.id) {
                try {
                    isChannel = getIsNewsletter(chat);
                } catch {
                    isChannel = false;
                }
                try {
                    isStatus = getIsBroadcast(chat);
                } catch {
                    isStatus = false;
                }
            }
        } catch {
            isChannel = false;
            isStatus = false;
        }`;

const unsafeLinkPreview = `        if (options.linkPreview) {
            delete options.linkPreview;
            const link = findLink(content);
            if (link) {
                let preview = await window
                    .require('WAWebLinkPreviewChatAction')
                    .getLinkPreview(link);
                if (preview && preview.data) {
                    preview = preview.data;
                    preview.preview = true;
                    preview.subtype = 'url';
                    options = { ...options, ...preview };
                }
            }
        }`;

const safeLinkPreview = `        if (options.linkPreview) {
            delete options.linkPreview;
            try {
                const link = findLink(content);
                if (link) {
                    let preview = await window
                        .require('WAWebLinkPreviewChatAction')
                        .getLinkPreview(link);
                    if (preview && preview.data) {
                        preview = preview.data;
                        preview.preview = true;
                        preview.subtype = 'url';
                        options = { ...options, ...preview };
                    }
                }
            } catch {
                /* skip broken link preview on newer WA Web */
            }
        }`;

if (!alreadyV3) {
  if (src.includes(unsafeSendMessageStart)) {
    src = src.replace(unsafeSendMessageStart, safeSendMessageStart);
  } else if (!src.includes("PATCHED_SEND_MESSAGE_V3")) {
    console.warn("[patch-wwebjs] sendMessage block not found — v3 patch skipped");
  }

  if (src.includes(unsafeLinkPreview)) {
    src = src.replace(unsafeLinkPreview, safeLinkPreview);
  } else if (!src.includes("PATCHED_SEND_MESSAGE_V3")) {
    console.warn("[patch-wwebjs] linkPreview block not found — v3 patch skipped");
  }
}

fs.writeFileSync(utilsPath, src, "utf8");
const parts = [];
if (src.includes("PATCHED_CONTACT_GETTER_V2")) parts.push("v2");
if (src.includes("PATCHED_SEND_MESSAGE_V3")) parts.push("v3");
console.log(`[patch-wwebjs] Patched Utils.js (${parts.join(" + ") || "partial"})`);

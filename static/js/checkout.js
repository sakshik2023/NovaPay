(function () {
  const co = document.getElementById('co');
  if (!co || !document.getElementById('step-pay')) return;
  const $ = (id) => document.getElementById(id);
  const csrf = () => (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  let tab = 'card', currentPayment = null, returnUrl = '';

  // ---------- helpers
  const network = (n) => /^4/.test(n) ? 'visa' : /^(5[1-5]|2[2-7])/.test(n) ? 'mastercard' : /^(34|37)/.test(n) ? 'amex' : /^(60|65|81|82|508)/.test(n) ? 'rupay' : 'card';
  const luhn = (n) => { let s = 0, alt = false; for (let i = n.length - 1; i >= 0; i--) { let d = +n[i]; if (alt) { d *= 2; if (d > 9) d -= 9; } s += d; alt = !alt; } return n.length >= 13 && s % 10 === 0; };
  const show = (id) => ['step-pay', 'step-otp', 'step-result'].forEach(s => $(s).classList.toggle('hidden', s !== id));
  const clearErrors = () => { document.querySelectorAll('[data-err]').forEach(e => e.textContent = ''); document.querySelectorAll('.field input.bad').forEach(i => i.classList.remove('bad')); $('globalErr').textContent = ''; };
  function fieldError(field, msg) {
    const el = document.querySelector('[data-err="' + field + '"]');
    if (el) { el.textContent = msg; const inp = el.previousElementSibling; if (inp) inp.classList.add('bad'); } else { $('globalErr').textContent = msg; }
  }
  async function post(url, body) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({ error: { description: 'Unexpected server response' } }));
    return { ok: r.ok, data: j };
  }

  // ---------- tabs
  document.querySelectorAll('.seg button').forEach(b => b.addEventListener('click', () => {
    tab = b.dataset.tab; document.querySelectorAll('.seg button').forEach(x => x.classList.toggle('on', x === b));
    $('form-card').classList.toggle('hidden', tab !== 'card'); $('form-upi').classList.toggle('hidden', tab !== 'upi'); clearErrors();
  }));

  // ---------- card formatting + live preview
  const num = $('card_number'), exp = $('expiry'), nm = $('card_name');
  function fmtNumber() {
    const d = num.value.replace(/\D/g, '').slice(0, 19), net = network(d);
    let out = '';
    if (net === 'amex') out = [d.slice(0, 4), d.slice(4, 10), d.slice(10, 15)].filter(Boolean).join(' ');
    else out = d.replace(/(.{4})/g, '$1 ').trim();
    num.value = out;
    $('ccNum').textContent = (out + '•••• •••• •••• ••••'.slice(out.length)).slice(0, 23) || '•••• •••• •••• ••••';
    $('ccNet').textContent = net === 'card' ? 'CARD' : net.toUpperCase();
    $('cc').className = 'cc ' + net;
  }
  num.addEventListener('input', fmtNumber);
  num.addEventListener('blur', () => { const d = num.value.replace(/\s/g, ''); if (d && !luhn(d)) fieldError('card_number', 'Enter a valid card number'); });
  exp.addEventListener('input', () => { let d = exp.value.replace(/\D/g, '').slice(0, 4); if (d.length >= 3) d = d.slice(0, 2) + '/' + d.slice(2); exp.value = d; $('ccExp').textContent = d || 'MM/YY'; });
  nm.addEventListener('input', () => $('ccName').textContent = nm.value || 'YOUR NAME');

  // ---------- test credential chips
  document.querySelectorAll('.tc').forEach(b => b.addEventListener('click', () => {
    if (b.dataset.card) {
      document.querySelector('[data-tab=card]').click(); num.value = b.dataset.card; fmtNumber();
      exp.value = '12/30'; exp.dispatchEvent(new Event('input')); $('cvv').value = '123';
      if (!nm.value) { nm.value = 'Test Customer'; nm.dispatchEvent(new Event('input')); }
    } else { document.querySelector('[data-tab=upi]').click(); $('vpa').value = b.dataset.upi; }
    clearErrors();
  }));

  // ---------- submit payment
  async function pay(btn, body) {
    clearErrors(); btn.disabled = true; btn.classList.add('loading');
    const { ok, data } = await post(co.dataset.submit, body);
    btn.disabled = false; btn.classList.remove('loading');
    if (!ok) { const e = data.error || {}; fieldError(e.field || 'global', e.description || 'Something went wrong'); return; }
    handle(data);
  }
  $('form-card').addEventListener('submit', (e) => { e.preventDefault(); pay($('payCard'), { method: 'card', card_number: num.value, expiry: exp.value, cvv: $('cvv').value, email: $('email_card').value }); });
  $('form-upi').addEventListener('submit', (e) => { e.preventDefault(); pay($('payUpi'), { method: 'upi', vpa: $('vpa').value, email: $('email_upi').value }); });

  function handle(d) {
    currentPayment = d.payment_id; returnUrl = d.return_url || '';
    if (d.needs_otp) { show('step-otp'); document.querySelector('.otp').focus(); return; }
    result(d);
  }

  // ---------- OTP
  const otps = [...document.querySelectorAll('.otp')];
  otps.forEach((el, i) => {
    el.addEventListener('input', () => { el.value = el.value.replace(/\D/g, ''); if (el.value && otps[i + 1]) otps[i + 1].focus(); if (otps.every(o => o.value)) $('otpBtn').click(); });
    el.addEventListener('keydown', (e) => { if (e.key === 'Backspace' && !el.value && otps[i - 1]) otps[i - 1].focus(); });
    el.addEventListener('paste', (e) => { const t = (e.clipboardData.getData('text') || '').replace(/\D/g, '').slice(0, 6); if (t) { e.preventDefault(); t.split('').forEach((c, k) => otps[k] && (otps[k].value = c)); otps[Math.min(t.length, 5)].focus(); } });
  });
  $('otpBtn').addEventListener('click', async () => {
    const btn = $('otpBtn'); $('otpErr').textContent = ''; btn.disabled = true; btn.classList.add('loading');
    const { ok, data } = await post(co.dataset.otp, { payment_id: currentPayment, otp: otps.map(o => o.value).join('') });
    btn.disabled = false; btn.classList.remove('loading');
    if (!ok) { $('otpErr').textContent = (data.error || {}).description || 'Verification failed'; otps.forEach(o => { o.classList.add('shake'); o.value = ''; setTimeout(() => o.classList.remove('shake'), 400); }); otps[0].focus(); return; }
    handle(data);
  });

  // ---------- result screen
  const OK = '<svg viewBox="0 0 52 52"><circle cx="26" cy="26" r="24"/><path d="M14 27l8 8 16-17"/></svg>';
  const BAD = '<svg viewBox="0 0 52 52"><circle cx="26" cy="26" r="24"/><path d="M18 18l16 16M34 18L18 34"/></svg>';
  function result(d) {
    show('step-result');
    const box = $('resBox'), success = d.status === 'captured';
    box.className = 'res ' + (success ? 'ok' : 'bad');
    $('resIc').innerHTML = success ? OK : BAD;
    $('resTitle').textContent = success ? 'Payment successful' : 'Payment failed';
    $('resMsg').textContent = success ? 'Thank you! Your payment has been received.' : (d.error || 'The payment could not be completed.');
    $('resCard').innerHTML = '<span class="muted">Amount</span><b>' + co.dataset.amount + '</b><span class="muted">Payment ID</span><b class="mono">' + d.payment_id + '</b>' + (success ? '' : '<span class="muted">Code</span><b>' + (d.error_code || '-') + '</b>');
    const ret = $('resReturn'), retry = $('resRetry');
    ret.classList.toggle('hidden', !returnUrl);
    if (returnUrl) { ret.href = returnUrl; ret.textContent = success ? 'Continue' : 'Back to merchant'; if (success) setTimeout(() => { window.location.href = returnUrl; }, 4500); }
    retry.classList.toggle('hidden', success);
    if (success) confetti();
  }
  $('resRetry').addEventListener('click', () => { show('step-pay'); otps.forEach(o => o.value = ''); clearErrors(); });

  function confetti() {
    const colors = ['#5b4bff', '#00c9a7', '#ff6b8b', '#ffb020', '#8e6bff'];
    for (let i = 0; i < 70; i++) {
      const c = document.createElement('i'); c.className = 'confetti';
      c.style.left = Math.random() * 100 + 'vw'; c.style.background = colors[i % colors.length];
      c.style.animationDuration = 2 + Math.random() * 2.2 + 's'; c.style.animationDelay = Math.random() * .6 + 's';
      document.body.appendChild(c); setTimeout(() => c.remove(), 5200);
    }
  }
})();

// collapse the sandbox credentials panel on small screens so the payment form is visible first
(function () { const d = document.querySelector('.co-test'); if (d && window.innerWidth < 860) d.removeAttribute('open'); })();

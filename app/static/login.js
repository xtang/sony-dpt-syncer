'use strict';
document.querySelector('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector('button');
  const error = document.querySelector('#login-error');
  button.disabled = true; error.textContent = '';
  try {
    const response = await fetch('/api/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:form.elements.namedItem('password').value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '暂时无法登录');
    window.location.assign('/');
  } catch (failure) { error.textContent = failure.message; }
  finally { button.disabled = false; }
});

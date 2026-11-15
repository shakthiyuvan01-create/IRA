const $ = (s) => document.querySelector(s);
let coins = 1240;
const toast = (msg) => { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 2600); };
$('#enterGame').onclick = () => { $('#splash').classList.add('hidden'); $('#game').classList.remove('hidden'); };
function showPage(id){ document.querySelectorAll('.page').forEach(p=>p.classList.remove('active')); $('#'+id).classList.add('active'); document.querySelectorAll('.nav').forEach(n=>n.classList.toggle('active',n.dataset.page===id)); }
document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>showPage(b.dataset.page));
document.querySelectorAll('.claim').forEach(b=>b.onclick=()=>{ const amount=+b.dataset.coins; coins+=amount; $('.wallet span').textContent='◈ '+coins.toLocaleString(); b.outerHTML='<button class="locked">CLAIMED</button>'; toast(`+${amount} coins added to your wallet`); });
$('#searchFriend').onclick=()=>{ const name=$('#friendInput').value.trim(); if(!name) return toast('Enter a player name or player ID'); $('#searchResult').classList.remove('hidden'); toast(`Player found: ${name.toUpperCase()}`); };
$('#sendRequest').onclick=()=>{ $('#sendRequest').textContent='REQUEST SENT'; $('#sendRequest').disabled=true; toast('Friend request sent to MAYA_RIVER'); };
$('#inviteBtn').onclick=()=>showPage('friends');
$('#createTeam').onclick=()=>toast('Team created — invite your friends to join.');
$('#trainingBtn').onclick=()=>showPage('mission');
$('#battleBtn').onclick=()=>toast('Complete the training round first to unlock Battle.');
$('#exitMission').onclick=()=>showPage('lobby');
$('#coinBox').onclick=()=>{ $('#coinBox').classList.add('hidden'); $('#missionCoins').textContent='50'; $('#objective').textContent='VISIT VENDING MACHINE'; $('#vendor').classList.remove('hidden'); toast('Coin box opened! +50 mission coins'); };
$('#vendor').onclick=()=>{ $('#vendor').classList.add('hidden'); $('#objective').textContent='REACH EXTRACTION'; $('#extract').classList.remove('hidden'); toast('Ammo and Ember Carbine acquired'); };
$('#extract').onclick=()=>{ $('#extract').classList.add('hidden'); $('#objective').textContent='TRAINING COMPLETE'; toast('Training complete! Battle mode is now ready.'); setTimeout(()=>showPage('lobby'),1600); };

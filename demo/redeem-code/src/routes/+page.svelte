<script lang="ts">
	let code = $state('');
	let message = $state('');

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		const response = await fetch('/api/redeem', {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ code, user: 'demo-user' })
		});
		const body = await response.json();
		message = response.ok ? `Redeemed: ${body.kit} (${body.items} items)` : `Refused: ${body.error}`;
	}
</script>

<h1>Redeem a code</h1>
<form onsubmit={submit}>
	<label>Code <input bind:value={code} required maxlength="64" /></label>
	<button>Redeem</button>
</form>
<p>{message}</p>

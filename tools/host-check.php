<?php
/**
 * Can this host run EspoCRM?
 *
 * Upload to your web root, visit it in a browser, read the verdict, then
 * DELETE IT. It reports your PHP version and settings, which is not something
 * to leave sitting on a public URL.
 *
 * Requirements are read from EspoCRM's own composer.json, not from memory.
 */

$checks = [];

// --- PHP version: the one that most often decides it ---
$php = PHP_VERSION;
$okVersion = version_compare($php, '8.3.0', '>=') && version_compare($php, '8.6.0', '<');
$checks[] = [
    'PHP version', $okVersion, $php,
    $okVersion ? '' : 'EspoCRM needs >= 8.3 and < 8.6. On cPanel try '
        . 'MultiPHP Manager or "Select PHP Version" before giving up.',
];

// --- extensions EspoCRM requires ---
foreach (['openssl','json','zip','gd','mbstring','xml','dom','curl','exif','pdo','ctype'] as $ext) {
    $has = extension_loaded($ext);
    $checks[] = ["ext-$ext", $has, $has ? 'loaded' : 'missing',
        $has ? '' : 'Enable it in cPanel -> Select PHP Version -> Extensions.'];
}

// --- pdo_mysql specifically: pdo alone is not enough ---
$pdoMysql = extension_loaded('pdo_mysql');
$checks[] = ['ext-pdo_mysql', $pdoMysql, $pdoMysql ? 'loaded' : 'missing',
    $pdoMysql ? '' : 'Needed to talk to MySQL/MariaDB.'];

// --- settings that bite later rather than at install ---
$mem = ini_get('memory_limit');
$memBytes = (function ($v) {
    if ($v === '-1') return PHP_INT_MAX;
    $unit = strtolower(substr(trim($v), -1));
    $n = (int) $v;
    return match ($unit) { 'g' => $n * 1073741824, 'm' => $n * 1048576, 'k' => $n * 1024, default => $n };
})($mem);
$okMem = $memBytes >= 256 * 1048576;
$checks[] = ['memory_limit', $okMem, $mem, $okMem ? '' : 'Raise to at least 256M.'];

$upload = ini_get('upload_max_filesize');
$checks[] = ['upload_max_filesize', true, $upload, 'Attachments are capped by this.'];

$maxExec = (int) ini_get('max_execution_time');
$okExec = $maxExec === 0 || $maxExec >= 180;
$checks[] = ['max_execution_time', $okExec, $maxExec ?: 'unlimited',
    $okExec ? '' : 'Upgrades and rebuilds want 180s or more.'];

// --- exec(): Espo's job daemon uses it. There is a fallback, but knowing
//     matters, because the symptom of it being off is "email never arrives". ---
$disabled = array_map('trim', explode(',', (string) ini_get('disable_functions')));
$execOk = !in_array('exec', $disabled, true) && !in_array('proc_open', $disabled, true);
$checks[] = ['exec / proc_open', $execOk, $execOk ? 'available' : 'disabled',
    $execOk ? '' : 'Espo can still run jobs from cron, but its daemon cannot. '
        . 'Set the cron entry and check Administration -> Scheduled Jobs actually runs.'];

$writable = is_writable(__DIR__);
$checks[] = ['web root writable', $writable, $writable ? 'yes' : 'no',
    $writable ? '' : 'The installer writes config and cache here.'];

$failed = count(array_filter($checks, fn ($c) => !$c[1]));

header('Content-Type: text/html; charset=utf-8');
?><!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EspoCRM host check</title>
<style>
 body{font:15px/1.5 system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}
 table{border-collapse:collapse;width:100%}
 td,th{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #ddd;vertical-align:top}
 .ok{color:#1a7f4b;font-weight:600}.bad{color:#a33a2c;font-weight:600}
 .verdict{padding:1rem;border-radius:8px;margin:1rem 0}
 .go{background:#e6f4ec}.no{background:#fbeae7}
 .fix{color:#666;font-size:.9rem}
 .warn{background:#fff4e0;padding:.75rem;border-radius:8px;margin-top:2rem;font-size:.9rem}
</style>
<h1>Can this host run EspoCRM?</h1>
<div class="verdict <?= $failed ? 'no' : 'go' ?>">
<?php if (!$failed): ?>
  <strong>Yes.</strong> Everything EspoCRM requires is present. Create a MySQL
  database in cPanel, upload the EspoCRM zip, and run its installer.
<?php else: ?>
  <strong><?= $failed ?> problem<?= $failed === 1 ? '' : 's' ?>.</strong>
  Most are fixable in cPanel &mdash; see the notes. If the PHP version cannot be
  raised to 8.3, this host will not run EspoCRM and a small VPS is the answer.
<?php endif; ?>
</div>

<table>
<tr><th>Check</th><th>Result</th><th>Value</th></tr>
<?php foreach ($checks as [$name, $ok, $value, $fix]): ?>
<tr>
  <td><?= htmlspecialchars($name) ?></td>
  <td class="<?= $ok ? 'ok' : 'bad' ?>"><?= $ok ? 'OK' : 'FIX' ?></td>
  <td><?= htmlspecialchars((string) $value) ?>
      <?php if ($fix): ?><div class="fix"><?= htmlspecialchars($fix) ?></div><?php endif; ?></td>
</tr>
<?php endforeach; ?>
</table>

<p class="fix">Server: <?= htmlspecialchars($_SERVER['SERVER_SOFTWARE'] ?? 'unknown') ?></p>

<div class="warn">
  <strong>Delete this file when you are done.</strong> It reports your PHP
  configuration, which is a small gift to anyone scanning your site.
</div>

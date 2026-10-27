setInterval(function(){var el=document.getElementById('percent'),btn=document.getElementById('finish');if(el&&btn){var p=parseInt(el.textContent,10)||0;btn.disabled=p<80;}},200);

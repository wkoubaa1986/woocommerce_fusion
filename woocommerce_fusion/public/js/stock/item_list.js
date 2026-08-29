frappe.listview_settings['Item'] = {
    onload: function(listview) {
        // 🎯 Synchroniser UNIQUEMENT les articles cochés dans la liste
        // (demande 29/08/2026) — même verrou et même rapport que la synchro
        // de masse, seule la source change.
        listview.page.add_inner_button(__('Sync Sélection'), function() {
            const coches = (listview.get_checked_items() || []).map(d => d.name);
            if (!coches.length) {
                frappe.msgprint(__('Cochez d\'abord un ou plusieurs articles dans la liste.'));
                return;
            }
            frappe.confirm(
                __('Synchroniser {0} article(s) sélectionné(s) vers WooCommerce ?', [coches.length]),
                function() {
                    frappe.call({
                        method: 'woocommerce_fusion.tasks.sync_job.start_sync_selection',
                        args: { item_codes: JSON.stringify(coches) },
                        freeze: true,
                        freeze_message: __('Démarrage de la synchronisation…'),
                        callback: function(r) {
                            if (!r.message) return;
                            frappe.show_alert({
                                message: r.message.message || __('Synchronisation de la sélection démarrée'),
                                indicator: 'green'
                            });
                            listview.clear_checked_items && listview.clear_checked_items();
                            if (r.message.report_url) {
                                setTimeout(function() {
                                    frappe.set_route(r.message.report_url.replace('/app/', ''));
                                }, 1000);
                            }
                        }
                    });
                }
            );
        });

        // Ajouter le bouton personnalisé
        listview.page.add_inner_button(__('Sync Active Items'), function() {
            frappe.prompt([
                {
                    fieldname: 'sync_mode',
                    label: __('Sync Mode'),
                    fieldtype: 'Select',
                    options: 'all\nmodified',
                    default: 'modified',
                    reqd: 1,
                    description: __('<b>all</b> = Tous les articles actifs | <b>modified</b> = Articles modifiés uniquement')
                },
                {
                    fieldname: 'batch_size',
                    label: __('Batch Size'),
                    fieldtype: 'Int',
                    default: 50,
                    reqd: 1,
                    description: __('Nombre d\'items à traiter par batch')
                },
                {
                    fieldname: 'offset',
                    label: __('Offset'),
                    fieldtype: 'Int',
                    default: 0,
                    reqd: 0,
                    description: __('Position de départ (laisser vide pour 0)')
                }
            ],
            function(values) {
                frappe.call({
                    method: 'woocommerce_fusion.tasks.sync_job.start_sync',
                    args: {
                        batch_size: values.batch_size,
                        offset: values.offset || 0,
                        sync_mode: values.sync_mode
                    },
                    freeze: true,
                    freeze_message: __('Démarrage de la synchronisation...'),
                    callback: function(r) {
                        if (r.message) {
                            const mode_label = values.sync_mode === 'all' ? 'complète' : 'des items modifiés';
                            frappe.show_alert({
                                message: r.message.message || __('Synchronisation ' + mode_label + ' démarrée'),
                                indicator: 'green'
                            });
                            
                            // Ouvrir le rapport dans un nouvel onglet
                            if (r.message.report_url) {
                                setTimeout(function() {
                                    frappe.set_route(r.message.report_url.replace('/app/', ''));
                                }, 1000);
                            }
                        }
                    }
                });
            },
            __('Démarrer la synchronisation'),
            __('Démarrer')
            );
        });
        
        // Ajouter bouton pour voir le statut du lock
        listview.page.add_inner_button(__('Sync Status'), function() {
            frappe.call({
                method: 'woocommerce_fusion.tasks.sync_job.get_sync_lock_status',
                callback: function(r) {
                    if (r.message) {
                        if (r.message.locked) {
                            const msg = `
                                <div style="padding: 10px;">
                                    <h4>⚠️ Synchronisation en cours</h4>
                                    <p><b>Mode:</b> ${r.message.sync_mode}</p>
                                    <p><b>Report ID:</b> ${r.message.report_id}</p>
                                    <p><b>Démarrée à:</b> ${r.message.started_at}</p>
                                    <p><a href="${r.message.report_url}" target="_blank">Voir le rapport →</a></p>
                                </div>
                            `;
                            frappe.msgprint({
                                title: __('Sync Status'),
                                indicator: 'orange',
                                message: msg
                            });
                        } else {
                            frappe.show_alert({
                                message: __('✅ Aucune synchronisation en cours'),
                                indicator: 'green'
                            });
                        }
                    }
                }
            });
        });
    }
};
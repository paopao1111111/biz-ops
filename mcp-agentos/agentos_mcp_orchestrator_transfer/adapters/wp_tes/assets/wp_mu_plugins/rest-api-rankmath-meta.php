<?php
/**
 * Plugin Name: REST API Rank Math Meta
 * Description: Register Rank Math SEO meta fields for WP REST API read/write
 * Version: 1.1
 */

add_action('init', function () {
    $fields = [
        'rank_math_title',
        'rank_math_description',
        'rank_math_focus_keyword',
    ];
    foreach ($fields as $field) {
        register_meta('post', $field, [
            'type'              => 'string',
            'single'            => true,
            'show_in_rest'      => true,
            'sanitize_callback' => 'sanitize_text_field',
            'auth_callback'     => function () {
                return current_user_can('edit_posts');
            },
        ]);
    }
});

// Grant edit_post_meta capability to any user who can edit posts
add_filter('map_meta_cap', function ($caps, $cap, $user_id) {
    $meta_caps = ['edit_post_meta', 'delete_post_meta', 'add_post_meta'];
    $rank_math_keys = ['rank_math_title', 'rank_math_description', 'rank_math_focus_keyword'];

    if (in_array($cap, $meta_caps, true)) {
        if (user_can($user_id, 'edit_posts')) {
            return ['edit_posts'];
        }
    }
    return $caps;
}, 10, 3);

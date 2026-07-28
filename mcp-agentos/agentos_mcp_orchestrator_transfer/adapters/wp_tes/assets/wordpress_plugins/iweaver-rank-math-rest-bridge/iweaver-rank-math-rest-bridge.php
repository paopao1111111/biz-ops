<?php
/**
 * Plugin Name: iWeaver Rank Math REST Bridge
 * Plugin URI: https://www.iweaver.ai/
 * Description: Exposes Rank Math SEO meta fields for pages in the WordPress REST API so external publishing tools can write SEO values.
 * Version: 1.0.0
 * Author: iWeaver
 * License: GPL-2.0-or-later
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * Register Rank Math SEO fields on pages so they are writable via wp/v2/pages.
 */
add_action(
	'init',
	function () {
		$meta_fields = array(
			'rank_math_title',
			'rank_math_description',
			'rank_math_focus_keyword',
		);

		foreach ( $meta_fields as $field ) {
			register_post_meta(
				'page',
				$field,
				array(
					'single'            => true,
					'type'              => 'string',
					'show_in_rest'      => true,
					'sanitize_callback' => 'sanitize_text_field',
					'auth_callback'     => function () {
						return current_user_can( 'edit_pages' );
					},
				)
			);
		}
	}
);

/**
 * Enable the legacy keywords meta tag output in Rank Math if focus keywords exist.
 */
add_filter( 'rank_math/frontend/show_keywords', '__return_true' );

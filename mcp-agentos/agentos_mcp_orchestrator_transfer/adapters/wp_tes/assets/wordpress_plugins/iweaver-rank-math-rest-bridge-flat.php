<?php
/**
 * Plugin Name: iWeaver Rank Math REST Bridge Flat
 * Plugin URI: https://www.iweaver.ai/
 * Description: Exposes Rank Math SEO meta fields for pages in the WordPress REST API so external publishing tools can write SEO values.
 * Version: 1.0.0
 * Author: iWeaver
 * License: GPL-2.0-or-later
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

add_action(
	'init',
	function () {
		$meta_fields = array(
			'rank_math_title',
			'rank_math_description',
			'rank_math_focus_keyword',
		);

		$post_types = array( 'page', 'post' );
		foreach ( $post_types as $pt ) {
			foreach ( $meta_fields as $field ) {
				register_post_meta(
					$pt,
					$field,
					array(
						'single'            => true,
						'type'              => 'string',
						'show_in_rest'      => true,
						'sanitize_callback' => 'sanitize_text_field',
						'auth_callback'     => function () {
							return current_user_can( 'edit_posts' );
						},
					)
				);
			}
		}
	}
);

add_filter( 'rank_math/frontend/show_keywords', '__return_true' );

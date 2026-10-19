module ThemeSelector
  def theme_button
    find(:button, &method(:theme_button?))
  end

  def assert_no_theme_button
    assert_no_selector(:button, &method(:theme_button?))
  end

  private

  def theme_button?(button)
    [button.text, button["aria-label"], button["title"], button["value"]].compact.any? do |label|
      label.match?(/theme|appearance|colou?r (?:mode|scheme)/i)
    end
  end
end
